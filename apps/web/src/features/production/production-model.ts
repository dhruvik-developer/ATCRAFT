/**
 * Wire shapes and vocabulary for the production module.
 *
 * The status list is the state machine from docs/database.md section 7:
 *
 *   DRAFT -> APPROVED -> RELEASED -> MATERIAL_ISSUED -> IN_PROCESS
 *         -> COMPLETED -> CLOSED          (CANCELLED before IN_PROCESS)
 *
 * The screens read the predicates below rather than hard-coding which button
 * appears where, so an action that is illegal from the current state is never
 * offered at all. The API enforces the same machine; this only decides what is
 * worth showing.
 *
 * Quantities and money are decimal strings end to end. `toDecimal` is the only
 * thing that computes on them - a shortage worked out through an IEEE-754
 * double is how a shop floor gets sent to a bin that is already empty.
 *
 * Two contract facts the screens have to respect:
 *
 * 1. **Priority is an integer, not a word.** `ProductionOrderCreate.priority`
 *    defaults to 100 and lower means more urgent. There is no LOW/HIGH enum.
 * 2. **Orders and work orders carry ids, not codes.** `ProductionOrderSummary`
 *    names `item_id`, `warehouse_id` and `plant_id` and no codes or names for
 *    any of them. Only the component snapshot carries an `item_code`, because
 *    the kit list stores the part number as it was at release.
 */

import { useQuery } from '@tanstack/react-query'
import Decimal from 'decimal.js'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { toDecimal } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

export const PRODUCTION_STATUSES = [
  'DRAFT',
  'APPROVED',
  'RELEASED',
  'MATERIAL_ISSUED',
  'IN_PROCESS',
  'COMPLETED',
  'CLOSED',
  'CANCELLED',
] as const
export type ProductionStatus = (typeof PRODUCTION_STATUSES)[number]

/** The happy path, in order. CANCELLED is off it and is never drawn as a step. */
export const PRODUCTION_STEPS: readonly ProductionStatus[] = [
  'DRAFT',
  'APPROVED',
  'RELEASED',
  'MATERIAL_ISSUED',
  'IN_PROCESS',
  'COMPLETED',
  'CLOSED',
]

export const PRODUCTION_SOURCE_TYPES = ['MANUAL', 'MRP_SUGGESTION', 'SALES_ORDER'] as const
export type ProductionSourceType = (typeof PRODUCTION_SOURCE_TYPES)[number]

export const WORK_ORDER_STATUSES = ['PENDING', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'] as const
export type WorkOrderStatus = (typeof WORK_ORDER_STATUSES)[number]

/**
 * Priority is a plain integer on the wire, lower being more urgent, defaulting
 * to 100 (`ProductionOrderCreate`). These bands exist only so a grid can colour
 * it and a form can offer sensible presets; nothing is sent but the number.
 */
export const PRIORITY_DEFAULT = 100

export interface PriorityBand {
  value: number
  label: string
  variant: 'destructive' | 'warning' | 'secondary' | 'muted'
}

export const PRIORITY_BANDS: readonly PriorityBand[] = [
  { value: 10, label: 'Urgent', variant: 'destructive' },
  { value: 50, label: 'High', variant: 'warning' },
  { value: 100, label: 'Normal', variant: 'secondary' },
  { value: 200, label: 'Low', variant: 'muted' },
]

/** Anything past the last band reads as the least urgent one. */
const LOWEST_BAND: PriorityBand = { value: Number.MAX_SAFE_INTEGER, label: 'Low', variant: 'muted' }

/** The band a priority number falls into: the first band it is at or below. */
export function priorityBand(priority: number): PriorityBand {
  for (const band of PRIORITY_BANDS) {
    if (priority <= band.value) return band
  }
  return LOWEST_BAND
}

// ------------------------------------------------------ state predicates ----

export const isApprovable = (status: ProductionStatus): boolean => status === 'DRAFT'
export const isReleasable = (status: ProductionStatus): boolean => status === 'APPROVED'

/** Material can still be drawn while the order runs, not only at release. */
export const acceptsMaterialIssue = (status: ProductionStatus): boolean =>
  status === 'RELEASED' || status === 'MATERIAL_ISSUED' || status === 'IN_PROCESS'

/**
 * Material can also go back, and only while the job is open to stock movement.
 *
 * The API gates both directions on one set - `ISSUABLE_PRODUCTION_STATUSES` in
 * `production/models.py`, which is RELEASED, MATERIAL_ISSUED and IN_PROCESS -
 * so a return is refused on a COMPLETED, CLOSED or CANCELLED order exactly as an
 * issue is. That matters at the end of a job: leftovers have to be handed back
 * *before* completing or cancelling, never after. It is spelled out here rather
 * than aliased to `acceptsMaterialIssue` because issuing and returning are
 * separate rights and separate decisions; the day one of them opens up, this is
 * the line that changes.
 */
export const acceptsMaterialReturn = (status: ProductionStatus): boolean =>
  status === 'RELEASED' || status === 'MATERIAL_ISSUED' || status === 'IN_PROCESS'

/**
 * Output may be booked once the material is out, but booking does **not** move
 * the order on.
 *
 * `create_production_entry` accepts MATERIAL_ISSUED and IN_PROCESS and leaves
 * the status exactly where it found it; the only edge into IN_PROCESS in
 * `PRODUCTION_ORDER_FLOW` is `POST /production/{id}/start`. Believing otherwise
 * is what stranded an order at MATERIAL_ISSUED with no way to reach COMPLETED,
 * because completion is legal only from IN_PROCESS.
 */
export const acceptsProductionEntry = (status: ProductionStatus): boolean =>
  status === 'MATERIAL_ISSUED' || status === 'IN_PROCESS'

/** The one edge into IN_PROCESS, and the screen has to offer it explicitly. */
export const isStartable = (status: ProductionStatus): boolean => status === 'MATERIAL_ISSUED'

export const isCompletable = (status: ProductionStatus): boolean => status === 'IN_PROCESS'
export const isClosable = (status: ProductionStatus): boolean => status === 'COMPLETED'

export const CANCELLABLE_STATUSES: readonly ProductionStatus[] = [
  'DRAFT',
  'APPROVED',
  'RELEASED',
  'MATERIAL_ISSUED',
]
export const isCancellable = (status: ProductionStatus): boolean =>
  CANCELLABLE_STATUSES.includes(status)

// ---------------------------------------------------------- permissions -----

/**
 * Permissions this module's write paths actually require.
 *
 * `apps/api/src/modules/production/router.py` is the authority, and it does not
 * gate everything on the two order permissions the frontend catalogue happens
 * to carry: `POST /{id}/complete` wants `production_entry.create`, `POST
 * /{id}/cancel` wants `production_order.cancel` and `POST /{id}/start` wants
 * `work_order.manage`. Gating those on `production_order.close` hid the buttons
 * from the people entitled to press them and offered them to people who would
 * be refused.
 *
 * The last two have no entry in `lib/permissions.ts` yet, so they are named
 * once here rather than spelled out at each call site; they belong in `P` and
 * should move there when that file is next touched.
 */

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /production` (`ProductionOrderSummary`).
 *
 * Hand-written, so it is registered in `MIRRORS` in
 * `apps/api/tests/unit/test_frontend_contract_types.py`, which fails if a field
 * here is one the API no longer sends.
 */
export interface ProductionOrderRow {
  id: string
  code: string
  status: ProductionStatus
  item_id: string
  warehouse_id: string
  plant_id: string | null
  /** Where the job is charged; null is unallocated. Material issues inherit it. */
  cost_centre_id: string | null
  planned_quantity: DecimalString
  produced_quantity: DecimalString
  rejected_quantity: DecimalString
  scrap_quantity: DecimalString
  planned_start_date: string
  planned_end_date: string
  actual_start_date: string | null
  actual_end_date: string | null
  /** Lower is more urgent. Default 100. */
  priority: number
  source_type: ProductionSourceType
  source_id: string | null
  created_at: string
}

/** `GET /production/{id}` (`ProductionOrderDetail`). Registered in `MIRRORS` too. */
export interface ProductionOrderDetail extends ProductionOrderRow {
  bom_id: string | null
  routing_id: string | null
  approved_by_id: string | null
  approved_at: string | null
  closure_note: string | null
  notes: string | null
  /** Running totals the API maintains; the screen never re-adds them. */
  total_material_cost: DecimalString
  total_operation_cost: DecimalString
  total_cost: DecimalString
  version: number
  updated_at: string
  component_count?: number
  work_orders?: WorkOrderRow[]
}

/** One row of `GET /production/{id}/components` (`ComponentRead`). */
export interface ProductionComponent {
  id: string
  production_order_id: string
  line_number: number
  item_id: string
  /** The part number snapshotted onto the kit list at release. */
  item_code: string
  uom_id: string
  warehouse_id: string
  bom_level: number
  required_quantity: DecimalString
  issued_quantity: DecimalString
  returned_quantity: DecimalString
  /** Issued less returned - what is actually out on the floor. */
  net_issued_quantity: DecimalString
  consumed_quantity: DecimalString
  /**
   * Live coverage, read from allocation and inventory at request time rather
   * than stored (`ComponentRead`). All three are optional on the contract - a
   * payload that could not resolve them omits them - so nothing may read them
   * as though a missing figure meant zero.
   */
  reserved_quantity?: DecimalString
  /** Free stock the issue could still draw on, as the API computed it. */
  available_quantity?: DecimalString
  /** The API's own shortage: what cannot be covered from free stock today. */
  shortage_quantity?: DecimalString
  is_backflushed: boolean
}

/** One row of `GET /production/{id}/work-orders` (`WorkOrderRead`). */
export interface WorkOrderRow {
  id: string
  production_order_id: string
  code: string
  sequence: number
  operation_code: string
  operation_name: string
  work_center_id: string
  machine_id: string | null
  operator_user_id: string | null
  status: WorkOrderStatus
  planned_quantity: DecimalString
  completed_quantity: DecimalString
  rejected_quantity: DecimalString
  /** Paid once for the batch. */
  planned_setup_minutes: DecimalString
  /** Planned run minutes for the whole work-order quantity. */
  planned_run_minutes: DecimalString
  planned_start_at: string | null
  planned_end_at: string | null
  actual_start_at: string | null
  actual_end_at: string | null
  shift: string | null
  notes: string | null
  work_center_cost_per_hour: DecimalString
  work_center_setup_cost: DecimalString
  process_cost: DecimalString
}

/** One row of `GET /production/{id}/entries` (`ProductionEntryRead`). */
export interface ProductionEntryRow {
  id: string
  production_order_id: string
  work_order_id: string | null
  entry_date: string
  shift: string | null
  operator_user_id: string | null
  machine_id: string | null
  produced_quantity: DecimalString
  rejected_quantity: DecimalString
  rejection_reason: string | null
  /** A decimal string like every other measured value, not a JS number. */
  downtime_minutes: DecimalString
  /** The inventory receipt this booking posted, when it produced good output. */
  output_transaction_id: string | null
  notes: string | null
  created_at: string
}

/** `POST /production/{id}/entries` (`ProductionEntryResult`). */
export interface ProductionEntryResult {
  message: string
  entry: ProductionEntryRow
  order: ProductionOrderRow
  output_movement: PostedMovement | null
}

export interface PostedMovement {
  transaction_id: string
  transaction_type: string
  item_id: string
  warehouse_id: string
  quantity: DecimalString
  unit_cost: DecimalString
  total_cost: DecimalString
}

/** `GET /production/{id}/cost` - readable only with `production_cost.view`. */
export interface ProductionCost {
  production_order_id: string
  code: string
  status: ProductionStatus
  material_cost: DecimalString
  operation_cost: DecimalString
  total_cost: DecimalString
  /** Null until something has actually been produced to divide by. */
  cost_per_unit: DecimalString | null
  planned_quantity: DecimalString
  produced_quantity: DecimalString
  /** True once the order is closed and the costing is settled. */
  is_final: boolean
  /** Operations with no rate behind them, so the figure above is understated. */
  uncosted_operation_count: number
  materials?: ProductionCostMaterialLine[]
  operations?: ProductionCostOperationLine[]
}

export interface ProductionCostMaterialLine {
  component_id: string
  item_id: string
  item_code: string
  required_quantity: DecimalString
  issued_quantity: DecimalString
  returned_quantity: DecimalString
  net_issued_quantity: DecimalString
  unit_cost: DecimalString
  total_cost: DecimalString
}

export interface ProductionCostOperationLine {
  work_order_id: string
  work_center_id: string
  sequence: number
  operation_code: string
  status: WorkOrderStatus
  /** True when the minutes came off the clock rather than off the plan. */
  is_actual_time: boolean
  charged_minutes: DecimalString
  cost_per_hour: DecimalString
  setup_cost: DecimalString
  time_cost: DecimalString
  process_cost: DecimalString
  total_cost: DecimalString
}

// ------------------------------------------------------ material issues -----

export const MATERIAL_ISSUE_STATUSES = ['DRAFT', 'POSTED', 'CANCELLED'] as const
export type MaterialIssueStatus = (typeof MATERIAL_ISSUE_STATUSES)[number]

/**
 * `POST /production/{id}/material-issues` (`MaterialIssueDetail`).
 *
 * A picking document, and nothing more. The router is explicit: "Creates the
 * picking document. Nothing moves until it is posted." Stock leaves the store at
 * `POST /production/material-issues/{id}/post`, so a screen that stops after the
 * first call has drafted a kit list and moved nothing.
 */
export interface MaterialIssueDetail {
  id: string
  code: string
  production_order_id: string
  issue_type: 'ISSUE' | 'RETURN'
  status: MaterialIssueStatus
  issue_date: string
  warehouse_id: string
  posted_at: string | null
  posted_by_id: string | null
  reason: string
  notes: string | null
  line_count: number
  total_quantity: DecimalString
  total_cost: DecimalString
  created_at: string
  lines?: MaterialIssueLine[]
}

export interface MaterialIssueLine {
  id: string
  line_number: number
  component_id: string
  item_id: string
  warehouse_id: string
  location_id: string | null
  batch_id: string | null
  quantity: DecimalString
  unit_cost: DecimalString
  total_cost: DecimalString
  notes: string | null
}

/** `POST /production/material-issues/{id}/post` (`MaterialIssuePostResult`). */
export interface MaterialIssuePostResult {
  message: string
  issue: MaterialIssueDetail
  /** The order's status after the posting; a complete kit advances it itself. */
  order_status: ProductionStatus
  movements?: PostedMovement[]
  /** Components the posting still left short, when it was a partial issue. */
  outstanding_components?: ComponentShortage[]
}

/** `POST /production/{id}/release` (`ReleaseResult`). */
export interface ReleaseResult {
  message: string
  order: ProductionOrderRow
  bom_id: string
  bom_code: string
  routing_id: string | null
  component_count: number
  work_order_count: number
  /** False means the release went ahead with components it could not hold. */
  fully_reserved: boolean
  shortages?: ComponentShortage[]
}

export interface ComponentShortage {
  component_id: string
  item_id: string
  item_code: string
  warehouse_id: string
  required_quantity: DecimalString
  /** What the release did manage to hold. */
  reserved_quantity: DecimalString
  shortage_quantity: DecimalString
  detail: string | null
}

/** `POST /production/{id}/complete` (`CompleteResult`). */
export interface CompleteResult {
  message: string
  order: ProductionOrderRow
  consumed_component_count: number
  backflush_issue_code: string | null
  output_movement: PostedMovement | null
}

/** `POST /production/{id}/close` (`CloseResult`). */
export interface CloseResult {
  message: string
  order: ProductionOrderRow
  cost: ProductionCost
  released_reservation_count: number
}

/** Lookup row behind the machine select on a production booking. */
export interface MachineOption {
  id: string
  code: string
  name: string
}

/** Lookup row behind the operator select on a production booking. */
export interface OperatorOption {
  id: string
  full_name: string
  email: string
}

// -------------------------------------------------------------- derived -----

const ZERO = new Decimal(0)

/** What still has to be drawn for a component: required less what is out there. */
export function outstandingQuantity(component: ProductionComponent): Decimal {
  // Net of returns: material issued and then handed back is not on the floor and
  // has to be drawn again.
  const outstanding = toDecimal(component.required_quantity).minus(
    toDecimal(component.net_issued_quantity),
  )
  return outstanding.greaterThan(0) ? outstanding : ZERO
}

/**
 * The other direction: what this component could actually hand back.
 *
 * `net_issued_quantity` is issued less returned - the job's holding, and the
 * ceiling the API itself enforces on a return line. Consumption is taken off on
 * top of it, because material already built into the product is not on the floor
 * to give back. The API would take it: its check is against the holding alone,
 * and `_post_return_lines` then quietly trims `consumed_quantity` down to the new
 * holding - so a return keyed over this figure un-consumes material that was
 * really used, and the costing stops matching what was built.
 */
export function returnableQuantity(component: ProductionComponent): Decimal {
  const returnable = toDecimal(component.net_issued_quantity).minus(
    toDecimal(component.consumed_quantity),
  )
  return returnable.greaterThan(0) ? returnable : ZERO
}

/**
 * The shortage the shop floor cares about: what cannot be covered today.
 *
 * `shortage_quantity` is the API's own answer and is the one to show *whenever
 * it is present, zero included*. Zero is the answer for a component the release
 * already reserved in full: its stock is held for this order and is therefore
 * excluded from the free balance beside it, so recomputing
 * `outstanding - available` reports a fully-kitted line as short by everything
 * that is being held for it. Falling through on `=== 0` turned every covered
 * component on the page red.
 *
 * The recomputation survives only for a payload that omits the field
 * altogether, which the contract permits.
 */
export function componentShortage(component: ProductionComponent): Decimal {
  if (component.shortage_quantity !== undefined && component.shortage_quantity !== null) {
    const reported = toDecimal(component.shortage_quantity)
    return reported.greaterThan(0) ? reported : ZERO
  }
  const short = outstandingQuantity(component).minus(toDecimal(component.available_quantity))
  return short.greaterThan(0) ? short : ZERO
}

/** Planned minutes for a work order: setup once, plus the run time. */
export function plannedMinutes(workOrder: WorkOrderRow): Decimal {
  return toDecimal(workOrder.planned_setup_minutes).plus(toDecimal(workOrder.planned_run_minutes))
}

/**
 * Minutes actually spent, when both ends of the clock are recorded.
 *
 * Null - not zero - while the operation is still running or was never clocked:
 * an over-run cannot be judged against a number nobody wrote down.
 */
export function actualMinutes(workOrder: WorkOrderRow): Decimal | null {
  if (!workOrder.actual_start_at || !workOrder.actual_end_at) return null
  const started = Date.parse(workOrder.actual_start_at)
  const ended = Date.parse(workOrder.actual_end_at)
  if (Number.isNaN(started) || Number.isNaN(ended) || ended < started) return null
  return new Decimal(ended - started).div(60_000).toDecimalPlaces(2)
}

/**
 * Produced against planned, as a percentage for a progress bar.
 *
 * The division is decimal.js; `Number` appears only at the last step, where the
 * value becomes a CSS width and further precision means nothing.
 */
export function progressPercent(produced: DecimalString, planned: DecimalString): number {
  const target = toDecimal(planned)
  if (target.lessThanOrEqualTo(0)) return 0
  const pct = toDecimal(produced).div(target).times(100)
  return Math.min(100, Math.max(0, Number(pct.toDecimalPlaces(1).toFixed(1))))
}

// -------------------------------------------------------------- lookups -----

/**
 * Machines, for the machine field on a production booking.
 *
 * Soft-failing: a booking that records output must still be possible when the
 * machine register is not readable by this account, so the field simply offers
 * nothing rather than the dialog refusing to open.
 */
const MACHINE_REGISTER_PARAMS = {
  limit: LOOKUP_LIST_LIMIT,
  is_active: true,
  with_total: false,
  sort_by: 'code',
  sort_dir: 'asc',
} as const

export function useMachineOptions(enabled: boolean) {
  return useQuery({
    // From `queryKeys`, not a hand-written literal: the scoped sibling in
    // `work-order-model.ts` keys the same endpoint through the same factory,
    // and only a shared factory makes the two provably distinct - these params
    // carry no `work_center_id`, so the keys cannot collide.
    queryKey: queryKeys.routing.machines(MACHINE_REGISTER_PARAMS),
    queryFn: async () => {
      try {
        return (
          await api.getPage<MachineOption>('/routing/machines', {
            params: { ...MACHINE_REGISTER_PARAMS },
          })
        ).data
      } catch (error) {
        return emptyOnPermissionError<MachineOption>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}
