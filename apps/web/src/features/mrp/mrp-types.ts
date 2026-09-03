/**
 * Wire shapes and vocabulary for the MRP engine.
 *
 * These mirror `MrpRunRead`, `MrpSuggestionRead` and `MrpRequirementRead` in the
 * OpenAPI schema (`apps/api/src/modules/mrp/schemas.py`). They live in the
 * feature folder rather than `types/api.ts` because they are the payloads of a
 * handful of endpoints, not part of the transport contract every module shares.
 *
 * Quantities are decimal strings end to end. Counts (`demand_line_count`,
 * `horizon_days`, `lead_time_days`) are genuine integers and are the only
 * numbers here.
 *
 * Two things the API does NOT send, and which no screen may therefore claim: a
 * suggestion carries `item_code` but no item *name*, no unit of measure and no
 * warehouse - it is planned per item across the run's warehouse scope, not per
 * warehouse - and the run list row is narrower than the run detail.
 */

import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

export const MRP_RUN_STATUSES = ['QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'] as const
export type MrpRunStatus = (typeof MRP_RUN_STATUSES)[number]

/**
 * A run whose demand-line count exceeds `settings.mrp_sync_threshold_demand_lines`
 * is handed to Celery and answered with 202 (docs/database.md section 6). The mode
 * is recorded on the run, so the history can explain why one run answered
 * instantly and the next one queued.
 */
export const MRP_RUN_MODES = ['SYNC', 'ASYNC'] as const
export type MrpRunMode = (typeof MRP_RUN_MODES)[number]

export const SUGGESTION_TYPES = ['MANUFACTURE', 'PURCHASE', 'JOB_WORK', 'TRANSFER'] as const
export type SuggestionType = (typeof SUGGESTION_TYPES)[number]

export const SUGGESTION_STATUSES = ['OPEN', 'CONVERTED', 'IGNORED'] as const
export type SuggestionStatus = (typeof SUGGESTION_STATUSES)[number]

/** A run in one of these states is still moving and is worth polling. */
export const IN_FLIGHT_RUN_STATUSES: readonly MrpRunStatus[] = ['QUEUED', 'RUNNING']

export const isRunInFlight = (status: MrpRunStatus | undefined | null): boolean =>
  status !== undefined && status !== null && IN_FLIGHT_RUN_STATUSES.includes(status)

/** Only an OPEN suggestion can still be converted or ignored. */
export const isActionable = (status: SuggestionStatus): boolean => status === 'OPEN'

/** Badge weight per suggestion type. Neutral by design: none of these is a fault. */
export const SUGGESTION_TYPE_VARIANT: Record<
  SuggestionType,
  'default' | 'secondary' | 'outline' | 'muted'
> = {
  PURCHASE: 'default',
  MANUFACTURE: 'secondary',
  JOB_WORK: 'outline',
  TRANSFER: 'muted',
}

/** What converting a suggestion of each type actually creates downstream. */
export const CONVERSION_TARGET: Record<SuggestionType, string> = {
  PURCHASE: 'purchase requisitions',
  MANUFACTURE: 'production orders',
  JOB_WORK: 'job work orders',
  TRANSFER: 'stock transfers',
}

/**
 * Whether the API will convert a suggestion of this type at all.
 *
 * `DEFAULT_CONVERSION_TARGET` maps purchase, manufacture and job work; a
 * TRANSFER has no entry, so converting one is refused outright unless the caller
 * names a target - and the three targets on offer
 * (requisition / production order / job work order) are none of them a stock
 * transfer. Offering the button anyway just produces a rejection a planner
 * cannot act on, so a transfer is raised by hand instead.
 */
export const isConvertible = (type: SuggestionType): boolean => type !== 'TRANSFER'

/** Suggestions in a single bulk-ignore call - `MAX_BULK_SUGGESTIONS` in the API. */
export const MAX_BULK_IGNORE = 500

// ----------------------------------------------------------- wire shapes ----

export interface WarehouseOption {
  id: string
  code: string
  name: string
}

/**
 * `parameters` on the run: the inputs the service recorded verbatim when the run
 * row was built (`mrp/service.py`). JSONB, so every key is optional - a run
 * written before a key existed must read as "not reported" rather than as a
 * confident default.
 */
export interface MrpRunParameters {
  horizon_days?: number | null
  horizon_end?: string | null
  as_of?: string | null
  warehouse_ids?: string[] | null
  respect_safety_stock?: boolean | null
  manual_demand_lines?: number | null
  notes?: string | null
}

/**
 * `result_summary` on a finished run - the engine's `plan.stats`, plus the
 * warnings it also lifts onto `MrpRunRead.warnings`.
 *
 * Every field is optional for the same reason as above. Nothing here is a
 * quantity: `result_summary` is JSONB and a Decimal does not serialise into it,
 * so the late-supply *quantities* live only in the warning text (`engine.py`,
 * `_late_supply_warning`).
 */
export interface MrpRunSummaryStats {
  demand_line_count?: number | null
  demand_item_count?: number | null
  requirement_count?: number | null
  /** Items the run could not cover. This is the "items short" figure. */
  shortage_item_count?: number | null
  suggestion_count?: number | null
  explosion_count?: number | null
  /** Suggestions whose suggested order date has already passed. */
  late_suggestion_count?: number | null
  /**
   * Short items whose inbound supply lands inside the horizon but *after* it is
   * needed, so the time phasing refused to net it (docs/assumptions.md 21).
   */
  late_supply_item_count?: number | null
  /** False means the explosion ran on needed-by dates that were still moving. */
  demand_dates_settled?: boolean | null
  /** False means that netting stage could not run at all - the plan is an upper bound. */
  netted_against_incoming?: boolean | null
  netted_against_production?: boolean | null
  respected_safety_stock?: boolean | null
  suggestions_by_type?: Partial<Record<SuggestionType, number>> | null
  warnings?: string[] | null
}

/** One row of `GET /mrp/runs` (`MrpRunSummary`). Narrower than the detail. */
export interface MrpRunRow {
  id: string
  code: string
  status: MrpRunStatus
  mode: MrpRunMode
  horizon_days: number
  warehouse_ids: string[] | null
  started_at: string | null
  completed_at: string | null
  demand_line_count: number
  suggestion_count: number
  created_at: string
}

/** `GET /mrp/runs/{id}` and the 200 body of `POST /mrp/runs` (`MrpRunRead`). */
export interface MrpRunDetail extends MrpRunRow {
  celery_task_id: string | null
  error_message: string | null
  parameters: MrpRunParameters | null
  result_summary: MrpRunSummaryStats | null
  /**
   * Caveats the engine lifted out of `result_summary` so a client does not have
   * to know the shape of a JSONB blob to show them: a netting stage that could
   * not run, a make item with no bill of materials, and - the one a planner
   * chases - inbound supply inside the horizon that arrives after it is needed,
   * named with its quantity, its arrival date and the date it was needed.
   */
  warnings: string[]
  /** Exact per-status counts from the API. Never subject to COUNT_CEILING. */
  suggestion_counts_by_status: Partial<Record<SuggestionStatus, number>>
  /** Present on every run, whichever mode it ended up in. */
  status_url: string | null
}

/** The 202 body of `POST /mrp/runs`, after `api.post` unwraps the envelope. */
export interface MrpRunAccepted {
  job_id: string
  status: string
  status_url: string
}

export type MrpRunCreated = MrpRunDetail | MrpRunAccepted

export function isAcceptedRun(result: MrpRunCreated): result is MrpRunAccepted {
  return typeof (result as Partial<MrpRunAccepted>).job_id === 'string'
}

/**
 * One contribution to a merged requirement, as the engine writes it into the
 * `source_demands` JSONB (`engine.py`, `DemandBucket.add_source`).
 *
 * The engine consolidates a part that several sales orders and several BOM
 * branches all need into ONE suggestion (docs/database.md sections 5 and 6), and
 * this is what keeps that merge auditable. There are two shapes, told apart by
 * `source`: an independent demand line, and a contribution inherited from an
 * exploded parent.
 */
export interface MrpDirectSourceDemand {
  source: 'SALES_ORDER' | 'MANUAL'
  source_id: string | null
  source_line_id: string | null
  /** Sales order code, or null for a manual line. */
  code: string | null
  quantity: DecimalString
  required_date: string
}

export interface MrpBomSourceDemand {
  source: 'BOM'
  /** The parent this contribution descended from. */
  via_item_id: string
  via_item_code: string
  quantity: DecimalString
  required_date: string
  level: number
}

export type MrpSourceDemand = MrpDirectSourceDemand | MrpBomSourceDemand

export const isBomSourceDemand = (demand: MrpSourceDemand): demand is MrpBomSourceDemand =>
  demand.source === 'BOM'

/**
 * One row of `GET /mrp/runs/{id}/suggestions` (`MrpSuggestionRead`).
 *
 * The engine caps how many contributions it lists per requirement
 * (`MAX_SOURCE_DEMANDS_LISTED`), so a full-looking `source_demands` may still be
 * a truncation - only the requirement row carries the exact count.
 */
export interface MrpSuggestionRow {
  id: string
  run_id: string
  item_id: string
  item_code: string
  suggestion_type: SuggestionType

  required_quantity: DecimalString
  /** Free stock the run could plan against. */
  available_quantity: DecimalString
  /** Inbound supply that arrives in time to count (docs/assumptions.md 21). */
  incoming_quantity: DecimalString
  shortage_quantity: DecimalString
  /** Shortage after min-order-quantity and order-multiple rounding. */
  suggested_quantity: DecimalString

  required_date: string
  /** Required date less the item's lead time. */
  suggested_order_date: string
  /** The API's own verdict that the suggested order date has already passed. */
  is_late: boolean
  lead_time_days: number

  status: SuggestionStatus
  converted_to_type: string | null
  converted_to_id: string | null
  converted_at: string | null
  ignored_reason: string | null
  source_demands: MrpSourceDemand[] | null
}

/**
 * One row of `GET /mrp/shortage-report`.
 *
 * DERIVED from the generated contract rather than retyped. This row carries the
 * late-supply interval, which is the whole reason the time-phasing work exists:
 * a shortage with supply behind it that lands too late is a different situation
 * from a shortage with nothing coming, and the planner has to be able to see
 * which one they are looking at. Retyping it here would let the two drift, and
 * a drifted field renders as `undefined` rather than as an error.
 */
export type MrpRequirementRow = SchemaOf<'MrpRequirementRead'>

/** `POST /mrp/suggestions/{id}/convert` (`SuggestionConversionResult`). */
export interface MrpConversionResult {
  handled: boolean
  message: string
  created_document_id: string | null
  created_document_code: string | null
  created_document_type: string | null
  suggestion: MrpSuggestionRow
}

/** `POST /mrp/suggestions/bulk-ignore` (`BulkIgnoreResult`). */
export interface MrpBulkIgnoreResult {
  message: string
  ignored_count: number
  ignored_ids: string[]
  /** Suggestions that were no longer OPEN by the time the call landed. */
  skipped_ids: string[]
}

// --------------------------------------------------------------- helpers ----

/**
 * A count out of `result_summary`, or null when the run does not report it.
 *
 * Null means *unknown*, never zero: a run written before a stat existed, or one
 * that never finished, has nothing to say about it, and a tile must say so
 * rather than claim a confident nought.
 */
export function runStat(
  summary: MrpRunSummaryStats | null | undefined,
  key: keyof MrpRunSummaryStats,
): number | null {
  const value = summary?.[key]
  return typeof value === 'number' ? value : null
}

/** Suggestions of one type on a finished run, or null when not reported. */
export function suggestionsOfType(
  summary: MrpRunSummaryStats | null | undefined,
  type: SuggestionType,
): number | null {
  const byType = summary?.suggestions_by_type
  if (!byType) return null
  // The engine omits a type it raised nothing for, so a missing key here is a
  // real zero - unlike a missing `suggestions_by_type`, which is unknown.
  return byType[type] ?? 0
}

/**
 * The run id inside a 202 `status_url` such as `/api/v1/mrp/runs/{id}`.
 *
 * The job id and the run id are the same value for MRP, but the status URL is
 * the API's authoritative statement of what to poll, so it is preferred.
 */
export function runIdFromStatusUrl(statusUrl: string | null | undefined): string | null {
  if (!statusUrl) return null
  const path = statusUrl.split('?')[0] ?? ''
  const segments = path.split('/').filter(Boolean)
  return segments[segments.length - 1] ?? null
}

export function runDurationMs(run: {
  started_at: string | null
  completed_at: string | null
}): number | null {
  if (!run.started_at || !run.completed_at) return null
  const started = Date.parse(run.started_at)
  const completed = Date.parse(run.completed_at)
  if (Number.isNaN(started) || Number.isNaN(completed)) return null
  return Math.max(0, completed - started)
}

/** Compact elapsed time: "8s", "1m 12s", "1h 04m". */
export function formatDuration(ms: number | null): string {
  if (ms === null) return '-'
  const totalSeconds = Math.floor(ms / 1000)
  if (totalSeconds < 60) return `${totalSeconds}s`
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`
}
