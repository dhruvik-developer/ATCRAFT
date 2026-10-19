/**
 * Wire shapes for the inventory and allocation endpoints.
 *
 * Declared in the feature folder rather than in `types/api.ts` because these are
 * response payloads for four screens, not part of the transport contract every
 * module shares. Quantities and money are decimal strings throughout
 * (docs/api.md, "Numbers") and are never converted to a JS number for
 * arithmetic.
 *
 * Every interface here mirrors one response model in `openapi.json` field for
 * field. These are hand written rather than generated, so the rule is: a field
 * exists here only if the API sends it. A column bound to a field the API does
 * not send renders a silent zero - `toDecimal(undefined)` is `0` - rather than
 * failing, which is exactly how a screen goes wrong without the compiler
 * noticing.
 *
 * Shapes added since the generated contract landed are `SchemaOf<'...'>`
 * aliases instead: deriving turns a disagreement with the API into a compile
 * error, which is the whole argument in `types/api.ts`. The hand-written ones
 * above are left alone rather than churned under four live screens.
 */

import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------- lookups -----

export interface WarehouseOption {
  id: string
  code: string
  name: string
}

/**
 * `GET /warehouses` read whole, for a select.
 *
 * One spelling, so every screen that loads the list shares one cache entry
 * rather than fetching the same bounded table twice. Not `/warehouses/options`:
 * that is active-only, and a detail page has to resolve the code of a
 * warehouse that has since been retired.
 */
export const WAREHOUSE_LOOKUP_PARAMS = {
  limit: LOOKUP_LIST_LIMIT,
  sort_by: 'code',
  sort_dir: 'asc',
  with_total: false,
} as const

/** The subset of `ItemSummary` the pickers render. */
export interface ItemOption {
  id: string
  code: string
  name: string
}

// ------------------------------------------------------------ balances -----

/** One row of `GET /inventory/balances` (`BalanceRead`). */
export interface InventoryBalanceRow {
  id: string
  item_id: string
  item_code: string | null
  item_name: string | null
  warehouse_id: string
  warehouse_code: string | null
  location_id: string | null
  location_code: string | null
  batch_id: string | null
  batch_number: string | null

  on_hand_quantity: DecimalString
  reserved_quantity: DecimalString
  allocated_quantity: DecimalString
  in_transit_quantity: DecimalString
  qc_hold_quantity: DecimalString
  damaged_quantity: DecimalString
  job_work_quantity: DecimalString
  scrap_quantity: DecimalString
  /** Generated column: on_hand - reserved - qc_hold - damaged. */
  available_quantity: DecimalString

  average_cost: DecimalString
  stock_value: DecimalString
  /** Copied from the item master; null when the item has no reorder policy. */
  reorder_level: DecimalString | null
  last_movement_at: string | null
  updated_at: string | null
}

/**
 * Exactly the columns `BalanceRepository.sortable_fields` accepts. Sorting by
 * anything else is rejected outright and empties the screen, so every other
 * column is declared unsortable rather than left to the table's default.
 */
export const BALANCE_SORT_FIELDS: readonly string[] = [
  'on_hand_quantity',
  'available_quantity',
  'stock_value',
  'last_movement_at',
  'updated_at',
]

// -------------------------------------------------------------- ledger -----

/** The pool a movement landed in - `StockState` in the API. */
export type StockState =
  'AVAILABLE' | 'QC_HOLD' | 'DAMAGED' | 'IN_TRANSIT' | 'JOB_WORK' | 'SCRAP' | 'RETURN'

/** One row of `GET /inventory/ledger` (`LedgerEntryRead`). */
export interface InventoryLedgerRow {
  id: string
  transaction_type: string
  item_id: string
  item_code: string | null
  item_name: string | null
  warehouse_id: string
  warehouse_code: string | null
  location_id: string | null
  batch_id: string | null
  serial_id: string | null
  /** Signed: positive increases on-hand, negative decreases it. */
  quantity: DecimalString
  unit_cost: DecimalString
  total_cost: DecimalString
  balance_after: DecimalString
  stock_state: StockState
  reference_type: string
  reference_id: string | null
  reference_code: string | null
  reason: string
  occurred_at: string
  created_at: string
  created_by_id: string | null
}

/**
 * The transaction-type vocabulary from docs/database.md section 4.2. Each type
 * has a fixed sign, but the ledger row carries its own signed quantity, so the
 * sign shown to the user is always read from the row rather than from this list.
 */
export const INFLOW_TRANSACTION_TYPES = [
  'PURCHASE_RECEIPT',
  'PRODUCTION_RECEIPT',
  'JOB_WORK_RECEIPT',
  'MATERIAL_RETURN',
  'ASSEMBLY_RECEIPT',
  'CUSTOMER_RETURN',
  'TRANSFER_IN',
  'ADJUSTMENT_INCREASE',
  'OPENING_BALANCE',
  'QC_RELEASE',
] as const

export const OUTFLOW_TRANSACTION_TYPES = [
  'MATERIAL_ISSUE',
  'JOB_WORK_ISSUE',
  'DISPATCH',
  'PURCHASE_RETURN',
  'ASSEMBLY_CONSUME',
  'TRANSFER_OUT',
  'ADJUSTMENT_DECREASE',
  'SCRAP',
  'SCRAP_DISPOSAL',
  'QC_HOLD',
] as const

export const TRANSACTION_TYPES: readonly string[] = [
  ...INFLOW_TRANSACTION_TYPES,
  ...OUTFLOW_TRANSACTION_TYPES,
]

// -------------------------------------------------------- reservations -----

export type DemandType =
  'SALES_ORDER' | 'PRODUCTION_ORDER' | 'ASSEMBLY_ORDER' | 'JOB_WORK_ORDER' | 'SPARE' | 'TRANSFER'

export const DEMAND_TYPES: readonly DemandType[] = [
  'SALES_ORDER',
  'PRODUCTION_ORDER',
  'ASSEMBLY_ORDER',
  'JOB_WORK_ORDER',
  'SPARE',
  'TRANSFER',
]

/**
 * The select holds a plain string until something is chosen, and the request
 * body holds a `DemandType`. This is how the one becomes the other - a guard
 * the compiler can follow, rather than a cast asserting what a form control
 * "must" have produced.
 */
export function isDemandType(value: string): value is DemandType {
  return (DEMAND_TYPES as readonly string[]).includes(value)
}

/**
 * `ReservationStatus`. There is no CANCELLED member: a reservation that is
 * given up is RELEASED, and offering a status the enum does not have makes the
 * API reject the whole request rather than ignore the filter.
 */
export type ReservationStatus =
  'ACTIVE' | 'PARTIALLY_CONSUMED' | 'CONSUMED' | 'RELEASED' | 'EXPIRED'

export const RESERVATION_STATUSES: readonly ReservationStatus[] = [
  'ACTIVE',
  'PARTIALLY_CONSUMED',
  'CONSUMED',
  'RELEASED',
  'EXPIRED',
]

/** `ReservationType` - how firm the claim is. */
export type ReservationType = SchemaOf<'ReservationType'>

export const RESERVATION_TYPES: readonly ReservationType[] = ['HARD', 'SOFT']

/** As `isDemandType`, for how firm the claim is. */
export function isReservationType(value: string): value is ReservationType {
  return (RESERVATION_TYPES as readonly string[]).includes(value)
}

/**
 * `GET /allocation/available-to-promise` (`AvailableToPromiseRead`).
 *
 * Read from the balance table on every call and never cached: a stale "yes, we
 * have it" is an over-promise (master rules 34). It is shown while a
 * reservation is being typed so the planner can see what is free before
 * promising it - the API re-reads the position under a lock regardless, and
 * refuses with INSUFFICIENT_AVAILABLE_STOCK if the claim is too large.
 */
export type AvailableToPromise = SchemaOf<'AvailableToPromiseRead'>

/**
 * The body of `POST /allocation/reservations` (`ReservationCreate`).
 *
 * Derived, so the create form cannot quietly drift from what the endpoint
 * accepts. Note `priority` and `reservation_type` are required in this type
 * even though the API defaults them - a schema default is still a field the
 * server names, and stating both explicitly is what the form does anyway.
 */
export type ReservationCreateBody = SchemaOf<'ReservationCreate'>

/**
 * A demand id keyed by hand.
 *
 * `demand_id` is `format: uuid` in the contract, and this folder has no picker
 * for six different demand documents - a sales order, a production order and a
 * job work order all live in other modules. Validating the shape here at least
 * turns a mistyped id into a message beside the field rather than a 422 from
 * the server, the same trade `documents-page.tsx` makes for an entity id.
 */
export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** One row of `GET /allocation/reservations` (`ReservationRead`). */
export interface ReservationRow {
  id: string
  item_id: string
  item_code: string | null
  item_name: string | null
  warehouse_id: string
  warehouse_code: string | null
  location_id: string | null
  batch_id: string | null
  demand_type: DemandType
  demand_id: string
  demand_line_id: string | null
  demand_code: string | null
  reservation_type: 'HARD' | 'SOFT'
  status: ReservationStatus
  quantity: DecimalString
  consumed_quantity: DecimalString
  released_quantity: DecimalString
  remaining_quantity: DecimalString
  priority: number
  required_date: string | null
  expires_at: string | null
  reason: string
  released_by_id: string | null
  released_at: string | null
  release_reason: string | null
  created_at: string | null
  updated_at: string | null
  created_by_id: string | null
}

/**
 * A reservation the allocation engine considered taking stock from
 * (`ReallocationCandidateRead`).
 *
 * `reasons` is the engine's own account of why this donor ranked where it did -
 * it is never assembled in the browser. `score` is not shown as a number,
 * because it means nothing outside the ranking it came from.
 */
export interface ReallocationCandidate {
  reservation_id: string
  item_id: string
  warehouse_id: string
  demand_type: DemandType
  demand_id: string
  demand_code: string | null
  reservation_type: 'HARD' | 'SOFT'
  priority: number
  required_date: string | null
  quantity: DecimalString
  consumed_quantity: DecimalString
  remaining_quantity: DecimalString
  /** What this donor could actually give up, which is not its remaining claim. */
  takeable_quantity: DecimalString
  requires_approval: boolean
  score: DecimalString
  reasons: string[]
}

export type ReallocationStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'EXECUTED'

/**
 * `POST /allocation/reallocation-requests` (`ReallocationRequestRead`).
 *
 * There is no dry run. Ranking the donors and raising the request are one call,
 * because asking the client to post the winner back would leave a window in
 * which the chosen reservation is taken by somebody else. The response is the
 * request that was raised, against the donor the engine chose, plus the
 * alternatives it considered.
 */
export interface ReallocationRequest {
  id: string
  status: ReallocationStatus
  from_reservation_id: string
  from_demand_type: DemandType | null
  from_demand_id: string | null
  from_demand_code: string | null
  from_reservation_type: 'HARD' | 'SOFT' | null
  to_demand_type: DemandType
  to_demand_id: string
  to_demand_line_id: string | null
  to_demand_code: string | null
  item_id: string
  item_code: string | null
  item_name: string | null
  warehouse_id: string
  warehouse_code: string | null
  quantity: DecimalString
  reason: string
  approved_at: string | null
  decision_note: string | null
  to_reservation_id: string | null
  candidates: ReallocationCandidate[]
}

// ----------------------------------------------------------- movements -----

/** `TransferStatus`. */
export type TransferStatus = 'DRAFT' | 'APPROVED' | 'IN_TRANSIT' | 'RECEIVED' | 'CANCELLED'

/** One row of `GET /inventory/transfers` (`StockTransferRead`). */
export interface StockTransferRow {
  id: string
  code: string
  status: TransferStatus
  from_warehouse_id: string
  from_warehouse_code: string | null
  to_warehouse_id: string
  to_warehouse_code: string | null
  transfer_date: string
  expected_date: string | null
  dispatched_at: string | null
  received_at: string | null
  approved_at: string | null
  reason: string
  notes: string | null
  line_count: number
  total_quantity: DecimalString
  created_at: string
  updated_at: string
}

/** `AdjustmentStatus`. */
export type AdjustmentStatus =
  'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'POSTED' | 'REJECTED' | 'CANCELLED'

/**
 * One row of `GET /inventory/adjustments` (`StockAdjustmentRead`).
 *
 * There is no net-quantity total on the document. Lines can be in different
 * units, so the only figure that adds up across them is `total_value`.
 */
export interface StockAdjustmentRow {
  id: string
  code: string
  status: AdjustmentStatus
  warehouse_id: string
  cost_centre_id: string | null
  warehouse_code: string | null
  adjustment_date: string
  reason_code: AdjustmentReasonCode
  reason: string
  notes: string | null
  /**
   * The size of the movement, never its direction.
   *
   * Every adjustment line is costed as `abs(quantity) * unit_cost` and the
   * table carries a `total_value >= 0` CHECK, so a write-off and a write-on of
   * the same value are the same number here. Do not render it with a sign.
   */
  total_value: DecimalString
  /** Whether this document must go round the approval loop before it posts. */
  requires_approval: boolean
  approved_at: string | null
  posted_at: string | null
  decision_note: string | null
  line_count: number
  created_at: string
  updated_at: string
  created_by_id: string | null
}

/** `PhysicalCountStatus`. */
export type PhysicalCountStatus = 'DRAFT' | 'IN_PROGRESS' | 'COUNTED' | 'CONFIRMED' | 'CANCELLED'

/** One row of `GET /inventory/physical-counts` (`PhysicalCountRead`). */
export interface PhysicalCountRow {
  id: string
  code: string
  status: PhysicalCountStatus
  warehouse_id: string
  warehouse_code: string | null
  location_id: string | null
  count_date: string
  started_at: string | null
  counted_at: string | null
  confirmed_at: string | null
  confirmed_by_id: string | null
  line_count: number
  variance_line_count: number
  /** Signed money: what the variance is worth if the sheet is confirmed. */
  variance_value: DecimalString
  reason: string
  notes: string | null
  created_at: string
  updated_at: string
}

/**
 * One line of `GET /inventory/physical-counts/{count_id}/lines`
 * (`PhysicalCountLineRead`).
 *
 * Derived rather than restated. This is the only view of what a confirmation is
 * about to post, and a field that renders as a silent zero here understates a
 * write-off in the one place that matters.
 *
 * `variance_quantity` and `variance_value` are measured against the snapshot
 * taken when the sheet was opened. Confirmation re-reads the position under a
 * row lock and posts `counted - current`, so a line whose stock legitimately
 * moved since the snapshot posts a different figure - the preview says so
 * rather than presenting these as the settled numbers.
 */
export type PhysicalCountLineRow = SchemaOf<'PhysicalCountLineRead'>

/**
 * Whether the sheet can be confirmed.
 *
 * COUNTED is the only status `COUNT_FLOW` has an edge from to CONFIRMED.
 * DRAFT and IN_PROGRESS are deliberately excluded - a sheet nobody has finished
 * counting would post the snapshot back over itself - and CONFIRMED and
 * CANCELLED are terminal, so offering it there is a guaranteed invalid
 * transition the operator cannot tell from a real failure.
 */
export function acceptsCountConfirmation(count: Pick<PhysicalCountRow, 'status'>): boolean {
  return count.status === 'COUNTED'
}

/**
 * Whether the sheet can still be cancelled.
 *
 * `COUNT_FLOW` has an edge to CANCELLED from each of DRAFT, IN_PROGRESS and
 * COUNTED. Worth offering because `open_count_exists` refuses a new count for
 * the warehouse until its open sheet is finished or cancelled - without this a
 * mis-scoped draft locks the warehouse out of counting from the UI.
 */
export function acceptsCountCancellation(count: Pick<PhysicalCountRow, 'status'>): boolean {
  return count.status === 'DRAFT' || count.status === 'IN_PROGRESS' || count.status === 'COUNTED'
}

/**
 * `AdjustmentReason` - the vocabulary the API accepts, and nothing else. An
 * adjustment without a classified cause is exactly the kind of unexplained
 * stock change master rules 83 forbids, so the create form makes both the code
 * and a written reason mandatory.
 */
export const ADJUSTMENT_REASON_CODES = [
  'DAMAGE',
  'THEFT',
  'COUNT_VARIANCE',
  'EXPIRY',
  'CORRECTION',
  'OPENING',
] as const
export type AdjustmentReasonCode = (typeof ADJUSTMENT_REASON_CODES)[number]
