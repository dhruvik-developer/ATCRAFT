/**
 * Wire shapes, vocabulary and lookups for work orders - the operations a
 * production order is actually executed through.
 *
 * A production order says what is being made. A work order says where it is:
 * sequence 20, "Deburr", on a named work centre, optionally on a named machine,
 * against a named operator, with a planned window and a booked quantity. The
 * shop floor reads this list to find its next job; the planner reads the same
 * rows grouped by cell to find the bottleneck.
 *
 * Every type here is derived from the generated contract.
 * `apps/api/src/modules/production/schemas.py` is the author of these shapes and
 * a hand-written second opinion is how a screen comes to compile perfectly while
 * rendering nothing.
 *
 * Five properties of the contract shape the screens above it.
 *
 * 1. **Nothing is denormalised.** `WorkOrderRead` names `work_center_id`,
 *    `machine_id`, `operator_user_id` and `production_order_id` and carries no
 *    code or name for any of them. They are resolved through the bounded option
 *    lists at the bottom of this file - one cached lookup per session, never a
 *    request per row.
 * 2. **The list takes no `search`.** `work_order_filters` in the router offers
 *    job, status, work centre, machine, operator and `open_only`, and nothing
 *    else. `WorkOrderRepository.searchable_fields` exists but is not wired to
 *    this endpoint, so a search box would send a parameter the API silently
 *    ignores - a box that appears to work and does not, which is worse than no
 *    box at all.
 * 3. **Four fields sort, and only four.** `WorkOrderRepository.sortable_fields`
 *    is `sequence`, `status`, `planned_start_at`, `created_at`; anything else
 *    raises a validation error naming the allowed set. Every other column
 *    header is therefore fixed rather than sending a request that 422s.
 * 4. **Quantities, minutes and money are decimal strings** and stay strings all
 *    the way to the wire. `toDecimal` is the only thing that computes on them.
 * 5. **A machine belongs to exactly one work centre.** `update_work_order`
 *    calls `_assert_machine(..., work_center_id=work_order.work_center_id)` and
 *    refuses a machine from any other cell, so the machine list is only ever
 *    asked for scoped to the operation's own centre.
 */

import { useQuery } from '@tanstack/react-query'
import Decimal from 'decimal.js'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { toDecimal } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry has no work-order branch, so the keys live here in the
 * same hierarchical shape - but *under* the production prefix on purpose.
 *
 * Starting or completing an operation changes what the job looks like: the
 * order moves to IN_PROCESS on its first booking and its open-operation count
 * changes underneath the WIP board. A screen that invalidates
 * `queryKeys.production.all` must therefore pick these lists up too, which it
 * does only if they sit inside that prefix.
 */
export const workOrderKeys = {
  all: [...queryKeys.production.all, 'work-orders'] as const,
  list: (params: unknown) => [...queryKeys.production.all, 'work-orders', 'list', params] as const,
  /** `GET /production/{order_id}/work-orders` - the operations of one job. */
  ofOrder: (orderId: string, params: unknown) =>
    [...queryKeys.production.all, 'work-orders', 'of-order', orderId, params] as const,
  wipOrders: (params: unknown) => [...queryKeys.production.all, 'wip', 'orders', params] as const,
  wipWorkCentres: (params: unknown) =>
    [...queryKeys.production.all, 'wip', 'work-centres', params] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /production/work-orders` (`WorkOrderRead`). */
export type WorkOrder = SchemaOf<'WorkOrderRead'>

export type WorkOrderStatus = SchemaOf<'WorkOrderStatus'>

/** Body of `PATCH /production/work-orders/{id}` (`WorkOrderUpdate`). */
export type WorkOrderUpdatePayload = SchemaOf<'WorkOrderUpdate'>

/** Body of `POST /production/work-orders/{id}/status`. */
export type WorkOrderTransitionPayload = SchemaOf<'WorkOrderTransitionRequest'>

/**
 * One row of `GET /production/wip/work-centres` (`WipWorkCentreRow`).
 *
 * An aggregate over the open operations of open jobs, keyed by work centre and
 * carrying no centre code or name - and no id of its own, so the centre id is
 * the row identity.
 */
export type WipWorkCentreRow = SchemaOf<'WipWorkCentreRow'>

/** One live production order and its current net WIP valuation. */
export type WipOrderRow = SchemaOf<'WipOrderRow'>

/** One row of `GET /routing/work-centers` (`WorkCenterRead`). */
export type WorkCentreOption = SchemaOf<'WorkCenterRead'>

/** One row of `GET /routing/machines` (`MachineRead`). */
export type MachineOption = SchemaOf<'MachineRead'>

// ----------------------------------------------------------- API bounds -----

/** `WorkOrderUpdate.shift`: 1..16 characters, or null to clear it. */
export const MAX_SHIFT_LENGTH = 16

/** `WorkOrderUpdate.notes`. */
export const MAX_NOTES_LENGTH = 4000

// ------------------------------------------------------------ vocabulary ----

/**
 * The four statuses, as a list for the filter that offers them.
 *
 * `satisfies` keeps the two honest: a status the API drops stops compiling here
 * instead of surviving as an option nothing can ever match.
 */
export const WORK_ORDER_STATUSES = [
  'PENDING',
  'IN_PROGRESS',
  'COMPLETED',
  'CANCELLED',
] as const satisfies readonly WorkOrderStatus[]

/** COMPLETED and CANCELLED are `WORK_ORDER_FLOW.terminal` - no edge leads out. */
export const WORK_ORDER_TERMINAL_STATUSES: readonly WorkOrderStatus[] = ['COMPLETED', 'CANCELLED']

export const isTerminal = (status: WorkOrderStatus): boolean =>
  WORK_ORDER_TERMINAL_STATUSES.includes(status)

/**
 * Assignment and rescheduling stop at the terminal states.
 *
 * `update_work_order` refuses a COMPLETED or CANCELLED operation outright -
 * "is COMPLETED and can no longer be changed" - before it looks at the payload,
 * so an Assign button on a finished row is a form filled in for a 422.
 */
export const isReschedulable = (status: WorkOrderStatus): boolean => !isTerminal(status)

// -------------------------------------------------------- state machine -----

/** One legal edge out of the current status, with the copy that explains it. */
export interface WorkOrderTransition {
  target: WorkOrderStatus
  label: string
  /** What the transition actually does, shown before it is confirmed. */
  description: string
  destructive: boolean
  /** True when nothing leads back out of the target state. */
  irreversible: boolean
}

const START: WorkOrderTransition = {
  target: 'IN_PROGRESS',
  label: 'Start operation',
  description:
    'The operation is marked as running and stamped with a start time. Use "Happened at" if work actually began earlier.',
  destructive: false,
  irreversible: false,
}

const COMPLETE: WorkOrderTransition = {
  target: 'COMPLETED',
  label: 'Complete operation',
  description:
    'The operation is finished and stamped with an end time. A completed operation cannot be reopened, and its times are what the job is costed on.',
  destructive: false,
  irreversible: true,
}

const CANCEL: WorkOrderTransition = {
  target: 'CANCELLED',
  label: 'Cancel operation',
  description:
    'The operation is abandoned and drops off the shop-floor queue and the work-centre load. Quantities already booked against it stay booked.',
  destructive: true,
  irreversible: true,
}

/**
 * `WORK_ORDER_FLOW` in `apps/api/src/modules/production/service.py`, as edges:
 *
 *     PENDING     -> IN_PROGRESS | CANCELLED
 *     IN_PROGRESS -> COMPLETED   | CANCELLED
 *
 * with COMPLETED and CANCELLED terminal. The status endpoint takes any member
 * of `WorkOrderStatus`, so offering all four and letting the server decide
 * would compile, look complete and put an operator through a confirmation
 * dialog only to be refused by `assert_transition` at the end of it. The screen
 * offers what is legal *from the status in front of it* and nothing else.
 *
 * There is no PENDING -> COMPLETED edge. An operation that was done without
 * anyone starting it is started and then completed; the API stamps the missing
 * `actual_start_at` from the completion moment so the row stays honest.
 */
const TRANSITIONS: Record<WorkOrderStatus, readonly WorkOrderTransition[]> = {
  PENDING: [START, CANCEL],
  IN_PROGRESS: [COMPLETE, CANCEL],
  COMPLETED: [],
  CANCELLED: [],
}

export const transitionsFrom = (status: WorkOrderStatus): readonly WorkOrderTransition[] =>
  TRANSITIONS[status] ?? []

// -------------------------------------------------------------- derived -----

const ZERO = new Decimal(0)
const MINUTES_PER_HOUR = 60

/**
 * Planned minutes for the operation: setup paid once, plus the run time.
 *
 * The two are separate columns because they scale differently - setup is per
 * batch, run time is per unit - and the API charges them that way when it works
 * out the load on a cell. Added here only to say how long the operation is
 * expected to take.
 */
export function plannedMinutes(workOrder: WorkOrder): Decimal {
  return toDecimal(workOrder.planned_setup_minutes).plus(toDecimal(workOrder.planned_run_minutes))
}

/**
 * Booked against planned, as a percentage for a progress bar.
 *
 * The division is decimal.js; `Number` appears only at the last step, where the
 * value becomes a CSS width and further precision means nothing.
 */
export function bookedPercent(workOrder: WorkOrder): number {
  const planned = toDecimal(workOrder.planned_quantity)
  if (planned.lessThanOrEqualTo(0)) return 0
  const percent = toDecimal(workOrder.completed_quantity).div(planned).times(100)
  return Math.min(100, Math.max(0, Number(percent.toDecimalPlaces(1).toFixed(1))))
}

/**
 * The heaviest load on the page, which is what a load bar is scaled against.
 *
 * A bar is only ever a comparison, so it is scaled to the largest row actually
 * shown rather than to a capacity figure the endpoint does not report. Worked
 * out once for the page rather than once per row, or a full board turns a
 * cosmetic width into a few thousand decimal divisions.
 */
export function peakRemainingMinutes(rows: readonly WipWorkCentreRow[]): Decimal {
  return rows.reduce((highest, row) => Decimal.max(highest, toDecimal(row.remaining_minutes)), ZERO)
}

/** One centre's share of that peak, as a CSS width and never as a figure. */
export function loadShare(row: WipWorkCentreRow, peak: Decimal): number {
  if (peak.lessThanOrEqualTo(0)) return 0
  const share = toDecimal(row.remaining_minutes).div(peak).times(100)
  return Math.min(100, Math.max(0, Number(share.toDecimalPlaces(1).toFixed(1))))
}

/**
 * Minutes as hours and minutes: "3 h 20 m".
 *
 * Minutes arrive as a decimal string - `remaining_minutes` is pro-rated by what
 * is left to make and is rarely whole - so the rounding happens through
 * decimal.js and the value never becomes a double on the way to the screen.
 */
export function formatMinutes(value: DecimalString | Decimal | null | undefined): string {
  const total = (value instanceof Decimal ? value : toDecimal(value)).toDecimalPlaces(0)
  if (total.lessThanOrEqualTo(0)) return '0 m'
  const hours = total.dividedToIntegerBy(MINUTES_PER_HOUR)
  const minutes = total.minus(hours.times(MINUTES_PER_HOUR))
  if (hours.lessThanOrEqualTo(0)) return `${minutes.toFixed(0)} m`
  if (minutes.lessThanOrEqualTo(0)) return `${hours.toFixed(0)} h`
  return `${hours.toFixed(0)} h ${minutes.toFixed(0)} m`
}

/** Index a bounded option list by id, so a row resolves its label without a fetch. */
export function indexById<T extends { id: string }>(rows: readonly T[]): Map<string, T> {
  return new Map(rows.map((row) => [row.id, row]))
}

/** "MILL-01 - CNC milling", the way both the pickers and the grids name a record. */
export const optionLabel = (option: { code: string; name: string }): string =>
  `${option.code} - ${option.name}`

// -------------------------------------------------------------- lookups -----

/**
 * Work centres and machines, for the filters and the assignment form.
 *
 * Structural registers - tens of rows, not thousands - so one bounded page is
 * the whole list, and the key is built from the very object that is sent as the
 * query string. That is not decoration: `hooks/use-organisation-options.ts`
 * records what happens when several copies of a lookup share a key while
 * sending different parameters, and deriving one from the other makes the two
 * impossible to separate.
 *
 * A failure returns an empty list rather than propagating. The shop floor holds
 * `work_order.view` and frequently not `work_center.view` or `machine.view`; a
 * missing label is decoration on this screen, not the point of it, and the
 * queue must still be readable without it. Where a name cannot be resolved the
 * grids fall back to saying so rather than printing a raw uuid.
 */
const WORK_CENTRE_OPTION_PARAMS = {
  limit: LOOKUP_LIST_LIMIT,
  is_active: true,
  with_total: false,
  sort_by: 'code',
  sort_dir: 'asc',
} as const

export function useWorkCentreOptions(enabled = true) {
  return useQuery({
    queryKey: queryKeys.routing.workCenters(WORK_CENTRE_OPTION_PARAMS),
    queryFn: async (): Promise<WorkCentreOption[]> => {
      try {
        const page = await api.getPage<WorkCentreOption>('/routing/work-centers', {
          params: { ...WORK_CENTRE_OPTION_PARAMS },
        })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<WorkCentreOption>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

const MACHINE_OPTION_PARAMS = {
  limit: LOOKUP_LIST_LIMIT,
  is_active: true,
  with_total: false,
  sort_by: 'code',
  sort_dir: 'asc',
} as const

/**
 * The machines of one work centre.
 *
 * Always scoped, never the whole register: `_assert_machine` refuses a machine
 * that does not belong to the operation's own centre, so an unscoped list would
 * offer choices the API rejects. Retired machines are excluded for the same
 * reason a picker exists at all - the list is for choosing what runs next.
 *
 * Named for the scoping rather than `useMachineOptions`, which is the whole
 * register in `production-model.ts`. The two used to share that name one
 * directory apart with different signatures and different semantics, so an
 * import of the wrong one compiled wherever the argument happened to fit -
 * and here the wrong one silently drops the constraint the API enforces.
 */
export function useWorkCentreMachineOptions(workCentreId: string | null | undefined) {
  const params = { ...MACHINE_OPTION_PARAMS, work_center_id: workCentreId ?? '' }
  return useQuery({
    queryKey: queryKeys.routing.machines(params),
    queryFn: async (): Promise<MachineOption[]> => {
      try {
        const page = await api.getPage<MachineOption>('/routing/machines', { params })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<MachineOption>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled: Boolean(workCentreId),
  })
}
