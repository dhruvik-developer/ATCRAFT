/** The dashboard's separately loaded 30-day series; KPI tiles do not wait for it. */
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/primitives'
import { formatMoney, formatQuantity, toDecimal } from '@/lib/format'
import type { DecimalString } from '@/types/api'

interface DashboardTrendPoint {
  day: string
  value: DecimalString
}

interface DashboardTrendSeries {
  series: string
  points: DashboardTrendPoint[]
}

/** `GET /reports/dashboard/trends` (`DashboardTrendsRead`). */
export interface DashboardTrendsPayload {
  date_from: string
  date_to: string
  series: DashboardTrendSeries[]
  generated_at: string
}

// --------------------------------------------------------------- trends ----

/**
 * The four sparkline series, in display order, with the label and formatter each
 * one needs. The API returns the series it thinks this caller may read, so this
 * table is presentation only - a series missing from the payload is not drawn,
 * and never drawn as an empty line.
 */
const TREND_SERIES: {
  key: string
  label: string
  format: (value: DecimalString) => string
}[] = [
  {
    key: 'sales_invoiced',
    label: 'Sales invoiced',
    format: (value) => formatMoney(value, { compact: true }),
  },
  {
    key: 'collections',
    label: 'Collections',
    format: (value) => formatMoney(value, { compact: true }),
  },
  {
    key: 'dispatches',
    label: 'Dispatches',
    format: formatQuantity,
  },
  {
    key: 'production_output',
    label: 'Production output',
    format: formatQuantity,
  },
]

const SPARK_WIDTH = 132
const SPARK_HEIGHT = 34
const SPARK_PADDING = 2

/**
 * A small inline SVG line chart.
 *
 * Not `recharts`: this is thirty points and one path, and the chart library
 * costs a chunk boundary for a shape a dozen lines of SVG draw. It is also the
 * version that can be read by a test without a canvas or a resize observer.
 *
 * A flat series - thirty days of zero - is drawn along the middle rather than
 * the bottom, because a line pinned to the axis reads as missing data. The
 * `aria-label` carries the range and the total, which is what a reader who
 * cannot see the line needs from it.
 */
function Sparkline({
  points,
  label,
  total,
}: {
  points: DashboardTrendPoint[]
  label: string
  total: string
}) {
  const values = points.map((point) => toDecimal(point.value))
  const highest = values.reduce(
    (max, value) => (value.greaterThan(max) ? value : max),
    toDecimal(0),
  )
  const lowest = values.reduce((min, value) => (value.lessThan(min) ? value : min), toDecimal(0))
  const span = highest.minus(lowest)
  const step = values.length > 1 ? (SPARK_WIDTH - SPARK_PADDING * 2) / (values.length - 1) : 0

  const path = values
    .map((value, index) => {
      const ratio = span.isZero() ? 0.5 : value.minus(lowest).dividedBy(span).toNumber()
      const x = SPARK_PADDING + index * step
      const y = SPARK_HEIGHT - SPARK_PADDING - ratio * (SPARK_HEIGHT - SPARK_PADDING * 2)
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <svg
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      className="h-9 w-full text-primary"
      role="img"
      aria-label={`${label}: ${total} over the last ${points.length} days`}
      preserveAspectRatio="none"
    >
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}

export default function TrendCards({ trends }: { trends: DashboardTrendsPayload }) {
  const series = trends.series
  const bySeries = new Map(series.map((entry) => [entry.series, entry]))

  // No series at all means this account may read none of the four. The card is
  // absent rather than a wall of four empty lines.
  if (series.length === 0) return null

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Last 30 days</CardTitle>
        <p className="text-xs text-muted-foreground">
          One figure per day, from the documents posted on that day. A day with no activity is zero,
          not missing.
        </p>
      </CardHeader>
      <CardContent>
        <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
          {TREND_SERIES.map((config) => {
            const entry = bySeries.get(config.key)
            if (!entry) return null
            const total = entry.points.reduce(
              (sum, point) => sum.plus(toDecimal(point.value)),
              toDecimal(0),
            )
            const latest =
              entry.points.length > 0 ? (entry.points[entry.points.length - 1]?.value ?? '0') : '0'
            const totalText = total.toDecimalPlaces(2).toFixed()
            return (
              <div key={config.key}>
                <div className="flex items-baseline justify-between gap-2">
                  <p className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    {config.label}
                  </p>
                  <p className="shrink-0 text-sm font-semibold tabular-nums">
                    {config.format(latest)}
                  </p>
                </div>
                <Sparkline
                  points={entry.points}
                  label={config.label}
                  total={config.format(totalText)}
                />
                <p className="text-2xs text-muted-foreground">
                  Latest day, {config.format(totalText)} in total
                </p>
              </div>
            )
          })}
        </div>
      </CardContent>
    </Card>
  )
}
