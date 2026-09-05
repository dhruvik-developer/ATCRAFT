/**
 * Wire shapes for the report catalogue.
 *
 * The catalogue is what makes this screen honest: the API declares which reports
 * exist for this user, whether each one needs a period, and what its result
 * columns are. Nothing about a report's columns is hardcoded in the browser, so
 * a column added on the server appears here without a frontend release.
 *
 * Mirrors `apps/api/src/modules/reports/schemas.py`. Two things the descriptor
 * deliberately does *not* carry, and which this screen must therefore not
 * pretend to know:
 *
 * * **Per-report parameters.** Every report route binds the same
 *   `report_filters` dependency, so the filter set is uniform and is declared
 *   once here rather than per report.
 * * **Inline or background.** The server decides per request: a result inside
 *   the row limit comes back paginated, one past it comes back 202 with a job
 *   to poll. There is no flag to read in advance and no parameter to force
 *   either.
 */

/** `ReportKey` on the wire. The catalogue is the source of truth for what exists. */
export type ReportKey =
  | 'SALES_ORDER_STATUS'
  | 'STOCK_AVAILABILITY'
  | 'MRP_SHORTAGE'
  | 'PURCHASE_PENDING'
  | 'PRODUCTION_PENDING'
  | 'JOB_WORK_PENDING'
  | 'QC_REJECTION'
  | 'VENDOR_PERFORMANCE'
  | 'PRODUCT_PROFITABILITY'
  | 'FAST_MOVING'
  | 'SLOW_MOVING'
  | 'DEAD_STOCK'
  | 'ORDER_DELAY'
  | 'PRODUCTION_EFFICIENCY'
  | 'MACHINE_UTILISATION'
  | 'INVENTORY_VALUATION'
  | 'DISPATCH'
  | 'COURIER_TRACKING'
  | 'VENDOR_DISPATCH_HISTORY'
  | 'PACKING_LIST'

/**
 * A rendering hint derived on the server from the row model's field type.
 *
 * `number` covers both money and quantity: the value is an exact Decimal either
 * way and how it is formatted is a presentation decision, taken from the column
 * name below.
 */
export type ReportColumnKind = 'id' | 'bool' | 'number' | 'datetime' | 'date' | 'int' | 'text'

export interface ReportColumn {
  /** The key of this column in a result row. */
  name: string
  label: string
  kind: ReportColumnKind | string
}

export interface ReportDescriptor {
  key: ReportKey
  title: string
  description: string
  /** True when `date_from` and `date_to` are required - the API refuses without them. */
  requires_period: boolean
  columns: ReportColumn[]
}

/** One result row. Keys are the descriptor's column names. */
export type ReportRow = Record<string, string | number | boolean | null>

/**
 * The route a report key is served at.
 *
 * The API mounts each report at the kebab-case form of its key
 * (`STOCK_AVAILABILITY` -> `/reports/stock-availability`). Deriving it keeps a
 * report added on the server reachable without a hardcoded table here.
 */
export function reportPath(key: string): string {
  return `/reports/${key.toLowerCase().replace(/_/g, '-')}`
}

/**
 * How the catalogue is grouped on screen.
 *
 * The descriptor carries no business area, and inventing one from the key would
 * be a business rule made up in the browser. `requires_period` is a real,
 * API-declared distinction that also happens to be the one that changes how a
 * report is asked for, so it is what the picker groups by.
 */
export const PERIOD_GROUP = 'Over a period'
export const POSITION_GROUP = 'As things stand'

export function groupOf(descriptor: ReportDescriptor): string {
  return descriptor.requires_period ? PERIOD_GROUP : POSITION_GROUP
}
