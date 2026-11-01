import { RETURN_PERIOD_PATTERN } from './finance-types'

/**
 * Expand a filing month (`YYYY-MM`) into its inclusive calendar bounds.
 *
 * A return period is a calendar fact, not an instant, so calculate the final
 * day in UTC rather than passing the month through the browser's local zone.
 */
export function periodBounds(period: string): { from: string; to: string } | null {
  const match = RETURN_PERIOD_PATTERN.exec(period)
  if (!match) return null
  const [, year, month] = match
  const lastDay = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate()
  return {
    from: `${year}-${month}-01`,
    to: `${year}-${month}-${String(lastDay).padStart(2, '0')}`,
  }
}
