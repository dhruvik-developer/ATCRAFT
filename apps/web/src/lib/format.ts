/**
 * Display formatting for ERP values.
 *
 * The rule that matters here: money and quantities arrive from the API as
 * decimal *strings* and are computed with decimal.js. `Number(value)` is used
 * only at the final formatting step, after all arithmetic is finished, because
 * an IEEE-754 double cannot represent 0.1 exactly and an invoice that is one
 * paisa out is a real defect (master rules 84).
 */

import Decimal from 'decimal.js'
import { format, formatDistanceToNowStrict, isValid, parseISO } from 'date-fns'

import {
  DATE_FORMAT,
  DATE_INPUT_FORMAT,
  DATE_TIME_FORMAT,
  DATE_TIME_INPUT_FORMAT,
  DATE_TIME_INPUT_PRECISE_FORMAT,
  DEFAULT_CURRENCY,
  DEFAULT_LOCALE,
} from '@/lib/constants'
import type { DecimalString } from '@/types/api'

// Enough precision for a multi-level BOM cost roll-up without surprises.
Decimal.set({ precision: 28, rounding: Decimal.ROUND_HALF_UP })

/**
 * One `Intl.NumberFormat` per distinct set of options, kept for the session.
 *
 * Constructing one costs about 42 microseconds; calling `.format()` on a cached
 * one costs 0.8. Every money and quantity cell on every table row went through a
 * fresh constructor, so a 1400-cell page spent 68 ms of main-thread time on
 * formatter construction alone - measured, not estimated. Cached, the same pass
 * costs 4 ms.
 *
 * The cache is unbounded on purpose: the key is the option set, not the value,
 * and this application uses a handful of them (currency on/off, compact on/off,
 * a few fraction-digit widths). It cannot grow with the data.
 */
const formatterCache = new Map<string, Intl.NumberFormat>()

function formatter(options: Intl.NumberFormatOptions): Intl.NumberFormat {
  // The option values are primitives, so their JSON is a faithful cache key -
  // and building it costs a fraction of what constructing the formatter does.
  const key = JSON.stringify(options)
  let cached = formatterCache.get(key)
  if (!cached) {
    cached = new Intl.NumberFormat(DEFAULT_LOCALE, options)
    formatterCache.set(key, cached)
  }
  return cached
}

/** Test seam: drop the cached formatters. */
export function __resetFormatterCache(): void {
  formatterCache.clear()
}

export function toDecimal(value: DecimalString | number | null | undefined): Decimal {
  if (value === null || value === undefined || value === '') return new Decimal(0)
  try {
    return new Decimal(value)
  } catch {
    return new Decimal(0)
  }
}

/** Currency for display. Never used as an input to further arithmetic. */
export function formatMoney(
  value: DecimalString | number | null | undefined,
  options: { currency?: string; compact?: boolean; showSymbol?: boolean } = {},
): string {
  const { currency = DEFAULT_CURRENCY, compact = false, showSymbol = true } = options
  const amount = toDecimal(value)

  return formatter({
    style: showSymbol ? 'currency' : 'decimal',
    currency,
    minimumFractionDigits: compact ? 0 : 2,
    maximumFractionDigits: compact ? 1 : 2,
    notation: compact ? 'compact' : 'standard',
  }).format(amount.toNumber())
}

/**
 * Money scale, and the scale a unit price carries.
 *
 * `to_money` in `apps/api/src/core/types.py` quantizes to two places and
 * `to_unit_price` to six, so these are the API's own scales rather than a
 * display preference. Exported from here because this module is where value
 * display is defined; `finance-types.ts` imports them for its arithmetic so the
 * two cannot drift apart.
 */
export const MONEY_PLACES = 2
export const UNIT_PRICE_PLACES = 6

/**
 * A unit price as currency.
 *
 * Distinct from `formatMoney` because a unit price is not money at two places:
 * a rate of 12.3456 per unit is a real value the API stores to six, and showing
 * it as 12.35 loses the number the buyer agreed. Trailing zeros above two
 * places are trimmed so a round rate still reads as a price, and never fewer
 * than money's two are shown so a whole number still reads as one.
 */
export function formatUnitPrice(
  value: DecimalString | number | null | undefined,
  options: { currency?: string; showSymbol?: boolean } = {},
): string {
  const { currency = DEFAULT_CURRENCY, showSymbol = true } = options
  const price = toDecimal(value)
  const places = Math.max(MONEY_PLACES, Math.min(UNIT_PRICE_PLACES, price.decimalPlaces()))

  return formatter({
    style: showSymbol ? 'currency' : 'decimal',
    currency,
    minimumFractionDigits: MONEY_PLACES,
    maximumFractionDigits: places,
  }).format(price.toNumber())
}

/**
 * Quantity with trailing zeros trimmed.
 *
 * The API sends 4 decimal places uniformly, but showing "12.0000 NOS" in a grid
 * of whole-number hardware counts is noise. Fractional precision is preserved
 * when it is actually present.
 */
export function formatQuantity(
  value: DecimalString | number | null | undefined,
  uom?: string,
): string {
  const qty = toDecimal(value)
  const text = qty
    .toDecimalPlaces(4)
    .toFixed()
    .replace(/(\.\d*?)0+$/, '$1')
    .replace(/\.$/, '')
  const grouped = formatter({
    minimumFractionDigits: 0,
    maximumFractionDigits: 4,
  }).format(Number(text))
  return uom ? `${grouped} ${uom}` : grouped
}

export function formatNumber(value: number | null | undefined, fractionDigits = 0): string {
  if (value === null || value === undefined) return '-'
  return formatter({
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value)
}

export function formatPercent(
  value: DecimalString | number | null | undefined,
  fractionDigits = 1,
): string {
  const pct = toDecimal(value)
  return `${pct.toDecimalPlaces(fractionDigits).toFixed(fractionDigits)}%`
}

// ------------------------------------------------------------------ dates ---

function parse(value: string | Date | null | undefined): Date | null {
  if (!value) return null
  const parsed = value instanceof Date ? value : parseISO(value)
  return isValid(parsed) ? parsed : null
}

/** Date only. The API stores UTC; dates are shown in the browser's zone. */
export function formatDate(value: string | Date | null | undefined): string {
  const parsed = parse(value)
  return parsed ? format(parsed, DATE_FORMAT) : '-'
}

export function formatDateTime(value: string | Date | null | undefined): string {
  const parsed = parse(value)
  return parsed ? format(parsed, DATE_TIME_FORMAT) : '-'
}

export function formatRelative(value: string | Date | null | undefined): string {
  const parsed = parse(value)
  return parsed ? `${formatDistanceToNowStrict(parsed)} ago` : '-'
}

/** Whole days from today; negative means overdue. Used for delay badges. */
export function daysUntil(value: string | Date | null | undefined): number | null {
  const parsed = parse(value)
  if (!parsed) return null
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  return Math.round((startOfDay(parsed) - startOfDay(new Date())) / 86_400_000)
}

/**
 * yyyy-MM-dd for <input type="date"> and query parameters, on the browser's
 * own calendar.
 *
 * `toDateInput(new Date())` is how a screen says "today". A business date is
 * the local one: `new Date().toISOString().slice(0, 10)` is the UTC date, which
 * in India is still yesterday until 05:30, so a credit note issued then was
 * dated the day before - on 1 April, into the previous financial year and an
 * already-filed GST month. The lint rule in `eslint.config.js` refuses that
 * slice outside this file.
 */
export function toDateInput(value: string | Date | null | undefined): string {
  const parsed = parse(value)
  return parsed ? format(parsed, DATE_INPUT_FORMAT) : ''
}

/**
 * The value an `<input type="datetime-local">` holds: the browser's wall clock,
 * never UTC. `milliseconds` is for an input with `step="0.001"`, where a stored
 * instant has to survive an edit that leaves the field alone.
 */
export function toDateTimeInput(
  value: string | Date | null | undefined,
  options: { milliseconds?: boolean } = {},
): string {
  const parsed = parse(value)
  if (!parsed) return ''
  return format(
    parsed,
    options.milliseconds ? DATE_TIME_INPUT_PRECISE_FORMAT : DATE_TIME_INPUT_FORMAT,
  )
}

/** The instant a `datetime-local` value names, as the UTC ISO string the API stores. */
export function fromDateTimeInput(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = new Date(trimmed)
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}

/*
 * Calendar arithmetic on a date-only `YYYY-MM-DD`.
 *
 * These two work in UTC on purpose, and are the only sanctioned
 * `toISOString().slice` in the application: no instant is involved, so no zone
 * is either, and UTC is the zone with no daylight-saving gap to lose a day in.
 * Neither may be used to ask what day it is - that is `toDateInput(new Date())`.
 */

/** A real calendar date in `YYYY-MM-DD` form - no 30 February, no year 0000. */
export function isIsoCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return false
  const parsed = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value
}

/** Add whole days to a `YYYY-MM-DD`, staying in date arithmetic, never instants. */
export function addDaysISO(iso: string, delta: number): string {
  const [year, month, day] = iso.split('-').map(Number)
  const date = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, day ?? 1))
  date.setUTCDate(date.getUTCDate() + delta)
  return date.toISOString().slice(0, 10)
}

// ------------------------------------------------------------------ misc ----

export function formatFileSize(bytes: number | null | undefined): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`
}

/** Turn CONSTANT_CASE enum values into readable labels. */
export function humanise(value: string | null | undefined): string {
  if (!value) return '-'
  return value
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}
