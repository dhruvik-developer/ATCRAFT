/**
 * Wire shapes and vocabulary for the routing module.
 *
 * A routing is the "how" that sits beside the BOM's "what": an ordered list of
 * operations, each on a work centre and optionally a specific machine, each
 * carrying a setup time paid once per batch and a run time paid per unit.
 *
 * Two contract facts shape everything here:
 *
 * 1. **The rate is on the work centre, not the operation.** `RoutingOperationRead`
 *    carries times, a `process_cost` and no cost per hour; `WorkCenterRead`
 *    carries `cost_per_hour` and `setup_cost`. Costing an operation therefore
 *    means joining the two - which is exactly what `GET /routing/{id}/cost`
 *    already does, so the screens ask for that rather than multiplying a rate
 *    they do not have.
 * 2. **A routing lives at `/routing`, not `/routing/routings`,** and it names an
 *    `item_id` and no item code. Nothing in this module returns part numbers.
 *
 * Times and money are decimal strings on the wire and stay strings; `toDecimal`
 * is the only thing that computes on them.
 */

import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

export const ROUTING_STATUSES = ['DRAFT', 'APPROVED', 'OBSOLETE'] as const
export type RoutingStatus = (typeof ROUTING_STATUSES)[number]

/**
 * The machine condition vocabulary, as the API defines it. It is not
 * ACTIVE/IDLE/BLOCKED: those values are rejected outright by the filter and by
 * the status-change endpoint.
 */
export const MACHINE_STATUSES = [
  'AVAILABLE',
  'RUNNING',
  'MAINTENANCE',
  'BREAKDOWN',
  'RETIRED',
] as const
export type MachineStatus = (typeof MACHINE_STATUSES)[number]

export const MACHINE_STATUS_VARIANT: Record<
  MachineStatus,
  'success' | 'default' | 'warning' | 'destructive' | 'muted'
> = {
  AVAILABLE: 'success',
  RUNNING: 'default',
  MAINTENANCE: 'warning',
  BREAKDOWN: 'destructive',
  RETIRED: 'muted',
}

// ---------------------------------------------------------- permissions -----

/**
 * The three registers on this screen are three permissions, not one.
 *
 * `apps/api/src/modules/routing/router.py` gates every routing write on
 * `routing.manage`, every work-centre write on `work_center.manage` and every
 * machine write on `machine.manage`. Gating all three on `routing.manage`
 * offered a machine fitter buttons that would be refused and hid the work-centre
 * form from the maintenance planner entitled to use it.
 *
 * The latter two have no entry in `lib/permissions.ts` yet, so they are named
 * once here rather than spelled out at each call site; they belong in `P` and
 * should move there when that file is next touched.
 */

/** A draft is approved; an approved routing is retired. Nothing else is legal. */
export const isApprovable = (status: RoutingStatus): boolean => status === 'DRAFT'
export const isObsoletable = (status: RoutingStatus): boolean => status === 'APPROVED'
/** Only a draft still accepts edits to its operations. */
export const isEditable = (status: RoutingStatus): boolean => status === 'DRAFT'

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /routing` (`RoutingSummary`). */
export interface RoutingRow {
  id: string
  code: string
  name: string | null
  item_id: string
  version: number
  status: RoutingStatus
  is_default: boolean
  effective_from: string
  effective_to: string | null
  approved_by_id: string | null
  approved_at: string | null
  created_at: string
  updated_at: string
}

/** `GET /routing/{id}` - the header plus its operations, in sequence. */
export interface RoutingDetail extends RoutingRow {
  notes: string | null
  operations: RoutingOperation[]
}

/** One operation (`RoutingOperationRead`). */
export interface RoutingOperation {
  id: string
  routing_id: string
  sequence: number
  operation_code: string
  operation_name: string
  work_center_id: string
  machine_id: string | null
  /** Paid once per batch. */
  setup_time_minutes: DecimalString
  /** Paid for every unit produced. */
  run_time_per_unit_minutes: DecimalString
  /** Waiting before the operation can start - part of the lead time, not the cost. */
  queue_time_minutes: DecimalString
  /** Transit to the next work centre - likewise lead time, not cost. */
  move_time_minutes: DecimalString
  /** A flat charge on top of the time, for tooling, consumables or an outworker. */
  process_cost: DecimalString
  is_outsourced: boolean
  job_work_vendor_id: string | null
  qc_required: boolean
  notes: string | null
}

/** `GET /routing/{id}/cost` (`RoutingCostRead`) - the authoritative costing. */
export interface RoutingCost {
  routing_id: string
  routing_code: string
  item_id: string
  quantity: DecimalString
  total_minutes: DecimalString
  /** Minutes actually charged, after the work centre's working day is applied. */
  charged_minutes: DecimalString
  time_cost: DecimalString
  process_cost: DecimalString
  total_cost: DecimalString
  cost_per_unit: DecimalString
  lead_time_days: number
  working_hours_per_day: DecimalString
  operations: RoutingOperationCost[]
}

export interface RoutingOperationCost {
  sequence: number
  operation_code: string
  operation_name: string
  work_center_id: string
  /** The rate the operation was costed at, taken from its work centre. */
  work_center_cost_per_hour: DecimalString
  total_minutes: DecimalString
  charged_minutes: DecimalString
  time_cost: DecimalString
  process_cost: DecimalString
  total_cost: DecimalString
  is_outsourced: boolean
}

/**
 * One row of `GET /routing/work-centers` (`WorkCenterRead`).
 *
 * Derived rather than written out, so the cost centre a work centre's hours are
 * pooled into arrived with the contract instead of waiting to be copied here.
 * `capacity_per_hour` is units an hour; `setup_cost` is a flat charge per batch
 * on top of the setup time; `working_hours_per_day` is how the centre's minutes
 * convert into lead-time days.
 */
export type WorkCenter = SchemaOf<'WorkCenterRead'>

/** One row of `GET /routing/machines` (`MachineRead`). */
export interface Machine {
  id: string
  code: string
  name: string
  /** A machine always belongs to a work centre; the API has no unassigned seat. */
  work_center_id: string
  machine_type: string | null
  capacity_per_hour: DecimalString
  cost_per_hour: DecimalString
  status: MachineStatus
  last_maintenance_date: string | null
  next_maintenance_date: string | null
  is_active: boolean
  created_at: string
  updated_at: string
}
