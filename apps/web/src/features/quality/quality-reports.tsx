/**
 * Quality reporting: how one item behaves over time, and which vendors are
 * costing the plant money.
 *
 * Both series are aggregated by the API. A report screen that pulled every
 * inspection row and grouped it in the browser would be the exact anti-pattern
 * master rules 31 forbids, and quality history is one of the longest tables in
 * the system.
 *
 * Every chart is paired with the same figures as a table exposed to screen
 * readers. A colour-coded bar is not information anybody can act on if they
 * cannot see it.
 */

import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Factory, PackageSearch } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Link } from 'react-router-dom'

import { EmptyState } from '@/components/shared'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
} from '@/components/ui/primitives'
import { ItemPicker } from '@/features/inventory/pickers'
import { api } from '@/lib/api-client'
import { formatDate, formatNumber, formatPercent, formatQuantity, toDecimal } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'

import type { ItemQualityHistory as ItemQualityHistoryPayload, VendorQualitySummary as VendorQualitySummaryPayload } from './quality-types'

const AXIS_TICK = { fontSize: 11, fill: 'hsl(var(--muted-foreground))' }

/**
 * The window, in days.
 *
 * Both report endpoints take `window_days`; there is no month-based parameter,
 * and the earlier `months` value was simply ignored - every chart was drawn
 * over whatever default window the API chose.
 */
const PERIODS = [
  { value: '90', label: 'Last 90 days' },
  { value: '180', label: 'Last 180 days' },
  { value: '365', label: 'Last 365 days' },
] as const

interface TooltipEntry {
  dataKey?: string | number
  name?: string | number
  value?: number | string
  color?: string
}

/**
 * Recharts' default tooltip paints an inline white background, which is
 * unreadable in dark mode. This one wears the popover tokens instead.
 */
function ChartTooltip({
  active,
  payload,
  label,
  formatValue,
}: {
  active?: boolean
  payload?: TooltipEntry[]
  label?: string | number
  formatValue: (value: number, key: string) => string
}) {
  if (!active || !payload?.length) return null

  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-medium text-popover-foreground">
        {label === undefined ? '' : String(label)}
      </p>
      <ul className="space-y-0.5">
        {payload.map((entry) => (
          <li key={String(entry.dataKey)} className="flex items-center gap-2">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: entry.color }}
              aria-hidden
            />
            <span className="text-muted-foreground">{String(entry.name ?? entry.dataKey)}</span>
            <span className="ml-auto tabular text-popover-foreground">
              {formatValue(Number(entry.value ?? 0), String(entry.dataKey))}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The chart's figures as a real table, for screen readers and for copying. */
function ChartDataTable({
  caption,
  headers,
  rows,
}: {
  caption: string
  headers: string[]
  rows: string[][]
}) {
  return (
    <table className="sr-only">
      <caption>{caption}</caption>
      <thead>
        <tr>
          {headers.map((header) => (
            <th key={header} scope="col">
              {header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={row[0] ?? index}>
            {row.map((cell, cellIndex) => (
              <td key={`${cell}-${cellIndex}`}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ReportCard({
  title,
  description,
  action,
  children,
}: {
  title: string
  description: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>{title}</CardTitle>
            <CardDescription className="mt-1">{description}</CardDescription>
          </div>
          {action}
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

// ------------------------------------------------------- item history -------

function ItemQualityHistory({ windowDays }: { windowDays: string }) {
  const [itemId, setItemId] = useState<string | null>(null)

  // The item is a path segment, not a query parameter, and the window is in
  // days. The old `/quality/reports/item-quality?item_id=...` route does not
  // exist - this card was a 404 for every item chosen.
  const params = { window_days: Number(windowDays) }
  const history = useQuery({
    queryKey: queryKeys.reports.report('quality-item', { itemId, ...params }),
    queryFn: () =>
      api.get<ItemQualityHistoryPayload>(`/quality/reports/item/${itemId}`, { params }),
    enabled: Boolean(itemId),
    staleTime: staleTimes.dashboard,
  })

  const points = history.data?.periods ?? []
  // Recharts plots numbers, so the decimal strings are converted here - at the
  // very last step, after every figure has already been aggregated server-side.
  const series = points.map((point) => ({
    period: formatDate(point.period_start),
    accepted: toDecimal(point.accepted_quantity).toNumber(),
    rework: toDecimal(point.rework_quantity).toNumber(),
    rejected: toDecimal(point.rejected_quantity).toNumber(),
  }))

  return (
    <ReportCard
      title="Item quality history"
      description="How one item has been dispositioned over time. A rising rejected band on a bought part is a vendor conversation; on a made part it is a process one."
      action={
        <ItemPicker
          id="quality-report-item"
          label="Item"
          hideLabel
          value={itemId}
          onChange={setItemId}
          placeholder="Choose an item"
        />
      }
    >
      {!itemId && (
        <EmptyState
          icon={PackageSearch}
          title="Choose an item"
          description="Its inspection history over the selected window charts here."
        />
      )}

      {itemId && history.isLoading && <Skeleton className="h-[280px] w-full" />}

      {itemId && !history.isLoading && history.error && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the item history</AlertTitle>
          <AlertDescription>{history.error.message}</AlertDescription>
        </Alert>
      )}

      {itemId && !history.isLoading && !history.error && series.length === 0 && (
        <EmptyState
          title="No inspections in this window"
          description="Nothing has been inspected for this item over the period selected."
        />
      )}

      {series.length > 0 && (
        <>
          <div
            role="img"
            aria-label="Stacked bar chart of accepted, rework and rejected quantity per month for the selected item. The same figures are in the table that follows."
          >
            <ResponsiveContainer width="100%" height={280}>
              <BarChart
                data={series}
                margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
                accessibilityLayer
              >
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                <XAxis
                  dataKey="period"
                  tick={AXIS_TICK}
                  tickLine={false}
                  stroke="hsl(var(--border))"
                  interval={0}
                />
                <YAxis
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  width={64}
                  tickFormatter={(value: number) => formatQuantity(value)}
                />
                <Tooltip
                  cursor={{ fill: 'hsl(var(--muted))' }}
                  content={<ChartTooltip formatValue={(value) => formatQuantity(value)} />}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar
                  dataKey="accepted"
                  name="Accepted"
                  stackId="qty"
                  fill="hsl(var(--success))"
                  maxBarSize={56}
                />
                <Bar
                  dataKey="rework"
                  name="Rework"
                  stackId="qty"
                  fill="hsl(var(--warning))"
                  maxBarSize={56}
                />
                <Bar
                  dataKey="rejected"
                  name="Rejected"
                  stackId="qty"
                  fill="hsl(var(--destructive))"
                  radius={[4, 4, 0, 0]}
                  maxBarSize={56}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>

          <ChartDataTable
            caption="Inspected quantity by disposition and month"
            headers={[
              'Period',
              'Inspections',
              'Inspected',
              'Accepted',
              'Rework',
              'Rejected',
              'Rejection rate',
            ]}
            rows={points.map((point) => [
              formatDate(point.period_start),
              formatNumber(point.inspection_count),
              formatQuantity(point.inspected_quantity),
              formatQuantity(point.accepted_quantity),
              formatQuantity(point.rework_quantity),
              formatQuantity(point.rejected_quantity),
              formatPercent(point.rejection_rate),
            ])}
          />
        </>
      )}
    </ReportCard>
  )
}

// ----------------------------------------------------- vendor summary -------

/** Active vendors for the picker. A master table, bounded by design. */
interface VendorOption {
  id: string
  code: string
  name: string
}

function useVendorOptions() {
  return useQuery({
    queryKey: ['vendors', 'options'] as const,
    queryFn: async () => {
      try {
        return await api.get<VendorOption[]>('/vendors/options')
      } catch {
        return []
      }
    },
    staleTime: staleTimes.reference,
  })
}

/**
 * One vendor's quality record.
 *
 * This card used to rank every vendor worst-first. The endpoint behind it is
 * now per-vendor - `GET /quality/reports/vendor/{vendor_id}` - and there is no
 * cross-vendor quality ranking to replace it. Building one here would mean a
 * request per vendor and a sort in the browser, which is the aggregation this
 * screen exists to avoid, so the card asks which vendor instead.
 */
function VendorQualitySummary({ windowDays }: { windowDays: string }) {
  const [vendorId, setVendorId] = useState<string | null>(null)
  const vendors = useVendorOptions()

  const params = { window_days: Number(windowDays) }
  const summary = useQuery({
    queryKey: queryKeys.reports.report('quality-vendor', { vendorId, ...params }),
    queryFn: () =>
      api.get<VendorQualitySummaryPayload>(`/quality/reports/vendor/${vendorId}`, { params }),
    enabled: Boolean(vendorId),
    staleTime: staleTimes.dashboard,
  })

  const record = summary.data ?? null
  const items = record?.items ?? []
  const series = items.map((row) => ({
    item: row.item_id.slice(0, 8),
    rate: toDecimal(row.rejection_rate).toNumber(),
  }))

  return (
    <ReportCard
      title="Vendor quality record"
      description="What one vendor has actually shipped over the window, broken down by item. A high rate on one part is a tooling conversation; a high rate across their whole basket is a vendor one."
      action={
        <Select
          value={vendorId ?? ''}
          onValueChange={(value) => setVendorId(value || null)}
          disabled={vendors.isLoading}
        >
          <SelectTrigger className="w-[240px]" aria-label="Vendor">
            <SelectValue placeholder="Choose a vendor" />
          </SelectTrigger>
          <SelectContent>
            {(vendors.data ?? []).map((vendor) => (
              <SelectItem key={vendor.id} value={vendor.id}>
                {vendor.code} - {vendor.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      }
    >
      {!vendorId && (
        <EmptyState
          icon={Factory}
          title="Choose a vendor"
          description="Their inspection record over the selected window shows here."
        />
      )}

      {vendorId && summary.isLoading && <Skeleton className="h-[280px] w-full" />}

      {vendorId && !summary.isLoading && summary.error && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the vendor record</AlertTitle>
          <AlertDescription>{summary.error.message}</AlertDescription>
        </Alert>
      )}

      {vendorId && !summary.isLoading && !summary.error && record && !record.has_evidence && (
        <EmptyState
          icon={Factory}
          title="Nothing inspected in this window"
          description="No incoming inspection was raised against this vendor over the period selected, so there is no rate to report."
        />
      )}

      {record?.has_evidence && (
        <div className="space-y-4">
          <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-lg border p-3">
              <dt className="text-xs text-muted-foreground">Inspections</dt>
              <dd className="tabular text-lg font-semibold">
                {formatNumber(record.inspection_count)}
              </dd>
            </div>
            <div className="rounded-lg border p-3">
              <dt className="text-xs text-muted-foreground">Inspected</dt>
              <dd className="tabular text-lg font-semibold">
                {formatQuantity(record.inspected_quantity)}
              </dd>
            </div>
            <div className="rounded-lg border p-3">
              <dt className="text-xs text-muted-foreground">Rejected</dt>
              <dd className="tabular text-lg font-semibold text-destructive">
                {formatQuantity(record.rejected_quantity)}
              </dd>
            </div>
            <div className="rounded-lg border p-3">
              <dt className="text-xs text-muted-foreground">Rejection rate</dt>
              <dd className="tabular text-lg font-semibold text-destructive">
                {formatPercent(record.rejection_rate)}
              </dd>
            </div>
          </dl>

          {record.items_truncated && (
            <Alert variant="warning">
              <AlertTriangle aria-hidden />
              <AlertTitle>Item breakdown is incomplete</AlertTitle>
              <AlertDescription>
                The API returned only part of this vendor&apos;s item list. The headline figures
                above still cover the whole window.
              </AlertDescription>
            </Alert>
          )}

          {series.length > 0 && (
            <div
              role="img"
              aria-label="Bar chart of rejection rate by item for this vendor. The same figures are in the table that follows."
            >
              <ResponsiveContainer width="100%" height={Math.max(200, series.length * 32)}>
                <BarChart
                  data={series}
                  layout="vertical"
                  margin={{ top: 8, right: 16, bottom: 0, left: 0 }}
                  accessibilityLayer
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke="hsl(var(--border))"
                    horizontal={false}
                  />
                  <XAxis
                    type="number"
                    tick={AXIS_TICK}
                    tickLine={false}
                    stroke="hsl(var(--border))"
                    tickFormatter={(value: number) => `${value}%`}
                  />
                  <YAxis
                    type="category"
                    dataKey="item"
                    tick={AXIS_TICK}
                    tickLine={false}
                    axisLine={false}
                    width={100}
                  />
                  <Tooltip
                    cursor={{ fill: 'hsl(var(--muted))' }}
                    content={<ChartTooltip formatValue={(value) => formatPercent(value)} />}
                  />
                  <Bar
                    dataKey="rate"
                    name="Rejection rate"
                    fill="hsl(var(--destructive))"
                    radius={[0, 4, 4, 0]}
                    maxBarSize={18}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {items.length > 0 && (
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-sm">
                <caption className="sr-only">
                  This vendor&apos;s rejection rate by item for the selected window
                </caption>
                <thead className="bg-muted/50">
                  <tr>
                    <th
                      scope="col"
                      className="h-10 px-3 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      Item
                    </th>
                    <th
                      scope="col"
                      className="h-10 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      Inspections
                    </th>
                    <th
                      scope="col"
                      className="h-10 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      Inspected
                    </th>
                    <th
                      scope="col"
                      className="h-10 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      Rejected
                    </th>
                    <th
                      scope="col"
                      className="h-10 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      Scrapped
                    </th>
                    <th
                      scope="col"
                      className="h-10 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      Rate
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <tr key={row.item_id} className="border-t">
                      <td className="px-3 py-2">
                        {/* The breakdown carries item ids only. */}
                        <Link
                          to={`/items/${row.item_id}`}
                          className="font-mono text-xs hover:underline"
                        >
                          {row.item_id.slice(0, 8)}
                        </Link>
                      </td>
                      <td className="px-3 py-2 text-right tabular">
                        {formatNumber(row.inspection_count)}
                      </td>
                      <td className="px-3 py-2 text-right tabular">
                        {formatQuantity(row.inspected_quantity)}
                      </td>
                      <td className="px-3 py-2 text-right tabular">
                        {formatQuantity(row.rejected_quantity)}
                      </td>
                      <td className="px-3 py-2 text-right tabular">
                        {formatQuantity(row.scrap_quantity)}
                      </td>
                      <td className="px-3 py-2 text-right tabular font-medium text-destructive">
                        {formatPercent(row.rejection_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </ReportCard>
  )
}


// ------------------------------------------------------------------ tab -----

export function QualityReports() {
  const [windowDays, setWindowDays] = useState<string>('180')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end">
        <Select value={windowDays} onValueChange={setWindowDays}>
          <SelectTrigger className="w-[180px]" aria-label="Reporting window">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PERIODS.map((period) => (
              <SelectItem key={period.value} value={period.value}>
                {period.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <ItemQualityHistory windowDays={windowDays} />
      <VendorQualitySummary windowDays={windowDays} />
    </div>
  )
}
