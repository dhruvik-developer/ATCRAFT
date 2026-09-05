/**
 * Ageing by party, bucketed by days past due, one denomination at a time.
 *
 * The buckets are computed by the API against its own clock and an explicit
 * "as on" date - never in the browser, where a machine with a wrong date would
 * quietly reclassify a debt from healthy to ninety days late.
 *
 * This is a *report*, not a paginated list: `GET /finance/ageing` answers an
 * `ApiResponse<AgeingReport>` carrying one block per currency the company has
 * an open item in. Two rules from docs/assumptions.md section 27 shape this
 * screen and must not be softened:
 *
 * 1. **No figure ever mixes denominations.** Every total, tile, bar and cell on
 *    screen belongs to exactly one block and is formatted in that block's
 *    currency. There is no exchange-rate table behind this API, so a combined
 *    figure would be a number with no meaning.
 * 2. **Narrowing chooses what is restated, never what exists.** `currency`
 *    picks which block is at the top; the report still covers every other
 *    denomination. So a company that owes nothing in rupees and 40,000 USD is
 *    never shown an empty page - the other denominations are always listed, and
 *    a zero primary block says so out loud.
 *
 * Detail rows come back for at most five denominations while every denomination
 * reports its totals, so a block can legitimately have totals and no rows. That
 * block is shown as "totals only" with a control that makes it the primary one,
 * which is what fetches its parties.
 */

import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
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

import { DataTable } from '@/components/data-table'
import { StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney, formatNumber, toDateInput, toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import {
  financeKeys,
  type AgeingCurrencyBlock,
  type AgeingReport,
  type AgeingRow,
  type LedgerSide,
} from './finance-types'

type BucketKey = 'bucket_0_30' | 'bucket_31_60' | 'bucket_61_90' | 'bucket_90_plus'

/**
 * Bucket presentation.
 *
 * The colour is the message: money that has been late for three months is not
 * the same fact as money late by a fortnight, and an operator should not have
 * to read the column heading to know which is which.
 */
const BUCKETS: {
  key: BucketKey
  totalKey:
    'total_bucket_0_30' | 'total_bucket_31_60' | 'total_bucket_61_90' | 'total_bucket_90_plus'
  label: string
  fill: string
  fillOpacity: number
  tone: string
}[] = [
  {
    key: 'bucket_0_30',
    totalKey: 'total_bucket_0_30',
    label: '0-30 days',
    fill: 'hsl(var(--primary))',
    fillOpacity: 1,
    tone: '',
  },
  {
    key: 'bucket_31_60',
    totalKey: 'total_bucket_31_60',
    label: '31-60 days',
    fill: 'hsl(var(--warning))',
    fillOpacity: 0.55,
    tone: 'text-warning',
  },
  {
    key: 'bucket_61_90',
    totalKey: 'total_bucket_61_90',
    label: '61-90 days',
    fill: 'hsl(var(--warning))',
    fillOpacity: 1,
    tone: 'text-warning',
  },
  {
    key: 'bucket_90_plus',
    totalKey: 'total_bucket_90_plus',
    label: '90+ days',
    fill: 'hsl(var(--destructive))',
    fillOpacity: 1,
    tone: 'text-destructive font-medium',
  },
]

const CHART_ROWS = 10
const AXIS_TICK = { fontSize: 11, fill: 'hsl(var(--muted-foreground))' }
/** Parties per denomination. The API caps this at 500. */
const PARTY_LIMIT = 200

export function AgeingTab() {
  const [searchParams, setSearchParams] = useSearchParams()

  const partyType: LedgerSide = searchParams.get('party_type') === 'VENDOR' ? 'VENDOR' : 'CUSTOMER'
  const partyId = searchParams.get('party_id') ?? ''
  const asOf = searchParams.get('as_of') ?? ''
  const currency = searchParams.get('currency') ?? ''

  /**
   * Write every key a handler changes in ONE update.
   *
   * `setSearchParams` is not a React state setter: react-router builds the next
   * URL from the `searchParams` captured when the callback was created, then
   * navigates. Two calls in one handler both start from that same stale base
   * and the second navigation overwrites the first - which is how the
   * Receivables/Payables toggle used to clear `party_id` and silently leave
   * `party_type` on the side the reader had just switched away from, reporting
   * one ledger under the other's heading.
   */
  const updateParams = useCallback(
    (changes: Record<string, string | undefined>) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          for (const [key, value] of Object.entries(changes)) {
            if (value === undefined || value === '') next.delete(key)
            else next.set(key, value)
          }
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const params = useMemo(
    () => ({
      party_type: partyType,
      limit: PARTY_LIMIT,
      ...(partyId ? { party_id: partyId } : {}),
      ...(asOf ? { as_of: asOf } : {}),
      ...(currency ? { currency } : {}),
    }),
    [partyType, partyId, asOf, currency],
  )

  const report = useQuery({
    queryKey: financeKeys.ageing(params),
    queryFn: () => api.get<AgeingReport>('/finance/ageing', { params }),
    staleTime: staleTimes.transactional,
  })

  const data = report.data ?? null
  const blocks = useMemo(() => data?.currencies ?? [], [data])
  /**
   * `currencies[0]` is the block restated at the top level, and the API
   * guarantees it exists - zeroed rather than absent when nothing is open in
   * the requested denomination. The top-level `total_*` fields are the same
   * figures, so the block is read directly and nothing is recombined here.
   */
  const primary = blocks[0] ?? null
  const rows = useMemo(() => primary?.rows ?? [], [primary])

  /** Denominations that hold money the primary block does not speak for. */
  const otherWithMoney = useMemo(
    () => blocks.slice(1).filter((block) => !toDecimal(block.total_outstanding).isZero()),
    [blocks],
  )
  const primaryIsEmpty = primary ? toDecimal(primary.total_outstanding).isZero() : true

  /**
   * Chart geometry needs pixels, and pixels need a JS number. The conversion
   * happens once here, at the presentation edge, from a Decimal - no arithmetic
   * is done on the float afterwards (master rules 84). Every bar in the plot is
   * in `primary.currency`; the chart is redrawn when the denomination changes
   * rather than ever holding two.
   */
  const chartData = useMemo(
    () =>
      rows.slice(0, CHART_ROWS).map((row) => ({
        party: row.party_code || row.party_name,
        bucket_0_30: toDecimal(row.bucket_0_30).toNumber(),
        bucket_31_60: toDecimal(row.bucket_31_60).toNumber(),
        bucket_61_90: toDecimal(row.bucket_61_90).toNumber(),
        bucket_90_plus: toDecimal(row.bucket_90_plus).toNumber(),
      })),
    [rows],
  )

  const blockCurrency = primary?.currency ?? data?.currency ?? ''

  const columns = useMemo<ColumnDef<AgeingRow, unknown>[]>(
    () => [
      {
        id: 'party_name',
        header: 'Party',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <span className="block truncate font-medium">{row.original.party_name}</span>
            {row.original.party_code && (
              <span className="block truncate font-mono text-2xs text-muted-foreground">
                {row.original.party_code}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'oldest_due_date',
        header: 'Oldest due',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{formatDate(row.original.oldest_due_date)}</span>
        ),
      },
      {
        id: 'open_document_count',
        header: 'Open items',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {formatNumber(row.original.open_document_count)}
          </span>
        ),
      },
      {
        id: 'not_due',
        header: 'Not due',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const amount = toDecimal(row.original.not_due)
          return (
            <span className={amount.isZero() ? 'text-muted-foreground' : undefined}>
              {formatMoney(row.original.not_due, { currency: row.original.currency })}
            </span>
          )
        },
      },
      ...BUCKETS.map<ColumnDef<AgeingRow, unknown>>((bucket) => ({
        id: bucket.key,
        header: bucket.label,
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const amount = toDecimal(row.original[bucket.key])
          return (
            <span className={cn(amount.isZero() ? 'text-muted-foreground' : bucket.tone)}>
              {formatMoney(row.original[bucket.key], { currency: row.original.currency })}
            </span>
          )
        },
      })),
      {
        id: 'total_outstanding',
        header: 'Total',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.total_outstanding, { currency: row.original.currency })}
          </span>
        ),
      },
    ],
    [],
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <div
          className="inline-flex rounded-md border border-input p-0.5"
          role="group"
          aria-label="Ledger side"
        >
          {(
            [
              { value: 'CUSTOMER', label: 'Receivables' },
              { value: 'VENDOR', label: 'Payables' },
            ] as const
          ).map((side) => (
            <Button
              key={side.value}
              type="button"
              size="sm"
              variant={partyType === side.value ? 'default' : 'ghost'}
              aria-pressed={partyType === side.value}
              onClick={() => updateParams({ party_type: side.value, party_id: undefined })}
            >
              {side.label}
            </Button>
          ))}
        </div>

        {partyType === 'CUSTOMER' ? (
          <CustomerPicker
            id="ageing-customer"
            label="Customer"
            hideLabel
            value={partyId || null}
            onChange={(next) => updateParams({ party_id: next ?? undefined })}
          />
        ) : (
          <VendorPicker
            id="ageing-vendor"
            label="Vendor"
            hideLabel
            value={partyId || null}
            onChange={(next) => updateParams({ party_id: next ?? undefined })}
          />
        )}

        <div className="space-y-1.5">
          <Label htmlFor="ageing-as-of" className="text-xs">
            As on
          </Label>
          <Input
            id="ageing-as-of"
            type="date"
            className="w-[11rem]"
            value={asOf}
            max={toDateInput(new Date())}
            onChange={(event) => updateParams({ as_of: event.target.value || undefined })}
          />
        </div>
      </div>

      {report.error && !report.isLoading && (
        <Alert variant="destructive">
          <AlertTitle>The ageing report could not be loaded</AlertTitle>
          <AlertDescription>{report.error.message}</AlertDescription>
        </Alert>
      )}

      {/* Denominations. Always every one the company has an open item in, so a
          foreign-currency debt can never be hidden by the current narrowing. */}
      {blocks.length > 0 && (
        <CurrencyBar
          blocks={blocks}
          selected={blockCurrency}
          onSelect={(next) => updateParams({ currency: next })}
          isFetching={report.isFetching}
        />
      )}

      {primary && primaryIsEmpty && otherWithMoney.length > 0 && (
        <Alert variant="warning">
          <AlertTitle>Nothing is outstanding in {primary.currency}</AlertTitle>
          <AlertDescription>
            {otherWithMoney.length === 1
              ? 'One other denomination has open items:'
              : `${otherWithMoney.length} other denominations have open items:`}{' '}
            {otherWithMoney.map((block) => block.currency).join(', ')}. Amounts are never converted
            between them, so choose a denomination above to read its figures.
          </AlertDescription>
        </Alert>
      )}

      {primary && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 min-[1800px]:grid-cols-6">
          <StatTile
            label={`Not due (${primary.currency})`}
            value={formatMoney(primary.total_not_due, { currency: primary.currency })}
            hint="Open but inside terms"
            isLoading={report.isLoading}
          />
          {BUCKETS.map((bucket) => (
            <StatTile
              key={bucket.key}
              label={bucket.label}
              value={formatMoney(primary[bucket.totalKey], { currency: primary.currency })}
              tone={
                bucket.key === 'bucket_90_plus'
                  ? 'destructive'
                  : bucket.key === 'bucket_0_30'
                    ? 'default'
                    : 'warning'
              }
              isLoading={report.isLoading}
            />
          ))}
          <StatTile
            label={`Total (${primary.currency})`}
            value={formatMoney(primary.total_outstanding, { currency: primary.currency })}
            hint={`${formatNumber(primary.party_count)} parties, ${formatNumber(
              primary.document_count,
            )} open documents`}
            isLoading={report.isLoading}
          />
        </div>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">
            {partyType === 'CUSTOMER' ? 'Receivables' : 'Payables'} by age
            {blockCurrency ? ` (${blockCurrency})` : ''}
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            The {CHART_ROWS} parties with the largest outstanding in{' '}
            {blockCurrency || 'this denomination'}, stacked by how long the money has been overdue.
            Only this denomination is plotted - bars from two currencies cannot share an axis.
          </p>
        </CardHeader>
        <CardContent>
          {report.isLoading ? (
            <Skeleton className="h-[280px] w-full" />
          ) : chartData.length === 0 ? (
            <p className="py-16 text-center text-sm text-muted-foreground">
              {primary && primary.party_count > 0
                ? `Detailed parties are not returned for ${primary.currency} in this response. Choose it above to read them.`
                : 'Nothing is outstanding for these filters in this denomination.'}
            </p>
          ) : (
            <>
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 16 }}>
                  <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
                  <XAxis
                    type="number"
                    tick={AXIS_TICK}
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={(value: number) =>
                      formatMoney(value, { compact: true, currency: blockCurrency || undefined })
                    }
                  />
                  <YAxis
                    type="category"
                    dataKey="party"
                    width={120}
                    tick={AXIS_TICK}
                    tickLine={false}
                    axisLine={false}
                  />
                  <Tooltip
                    cursor={{ fill: 'hsl(var(--muted))', opacity: 0.4 }}
                    contentStyle={{
                      background: 'hsl(var(--popover))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: 'var(--radius)',
                      fontSize: 12,
                      color: 'hsl(var(--popover-foreground))',
                    }}
                    formatter={(value) =>
                      formatMoney(value as number, { currency: blockCurrency || undefined })
                    }
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  {BUCKETS.map((bucket) => (
                    <Bar
                      key={bucket.key}
                      dataKey={bucket.key}
                      name={bucket.label}
                      stackId="ageing"
                      fill={bucket.fill}
                      fillOpacity={bucket.fillOpacity}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>

              {/* The plot is a picture; this is the same numbers in a form a
                  screen reader can walk through. */}
              <table className="sr-only">
                <caption>Outstanding amount by party and age bucket, in {blockCurrency}</caption>
                <thead>
                  <tr>
                    <th scope="col">Party</th>
                    {BUCKETS.map((bucket) => (
                      <th key={bucket.key} scope="col">
                        {bucket.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(0, CHART_ROWS).map((row) => (
                    <tr key={`${row.party_id}-${row.currency}`}>
                      <th scope="row">{row.party_name}</th>
                      {BUCKETS.map((bucket) => (
                        <td key={bucket.key}>
                          {formatMoney(row[bucket.key], { currency: row.currency })}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </CardContent>
      </Card>

      {primary && primary.party_count > rows.length && (
        <p className="text-xs text-muted-foreground">
          Showing {formatNumber(rows.length)} of {formatNumber(primary.party_count)} parties in{' '}
          {primary.currency}. The totals above cover the whole denomination regardless; narrow by
          party to read one in full.
        </p>
      )}

      <DataTable
        columns={columns}
        data={rows}
        isLoading={report.isLoading}
        isFetching={report.isFetching}
        error={report.error as Error | null}
        onRefresh={() => void report.refetch()}
        getRowId={(row) => `${row.party_id}-${row.currency}`}
        emptyTitle="Nothing outstanding"
        emptyDescription={
          otherWithMoney.length > 0
            ? 'No party has an open balance in this denomination. Another one above does.'
            : 'No party has an open balance for these filters.'
        }
      />
    </div>
  )
}

/**
 * The denominations this report covers.
 *
 * Every block is offered, including the ones returned totals-only, because the
 * alternative - hiding a denomination the company genuinely owes money in - is
 * the failure the per-currency report shape exists to prevent. Selecting one
 * re-asks the API for it as the primary block, which is what returns its
 * parties.
 */
function CurrencyBar({
  blocks,
  selected,
  onSelect,
  isFetching,
}: {
  blocks: AgeingCurrencyBlock[]
  selected: string
  onSelect: (currency: string) => void
  isFetching: boolean
}) {
  const only = blocks.length === 1 ? blocks[0] : undefined
  if (only) {
    return (
      <p className="text-xs text-muted-foreground">
        All figures in <span className="font-medium text-foreground">{only.currency}</span>.{' '}
        {formatNumber(only.document_count)} open documents across {formatNumber(only.party_count)}{' '}
        parties.
      </p>
    )
  }

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Denomination">
        {blocks.map((block) => {
          const active = block.currency === selected
          return (
            <Button
              key={block.currency}
              type="button"
              size="sm"
              variant={active ? 'default' : 'outline'}
              aria-pressed={active}
              disabled={isFetching && !active}
              onClick={() => onSelect(block.currency)}
            >
              <span className="font-mono">{block.currency}</span>
              <span
                className={cn('ml-1.5 text-2xs', active ? 'opacity-80' : 'text-muted-foreground')}
              >
                {formatMoney(block.total_outstanding, { currency: block.currency, compact: true })}
              </span>
              {(block.rows?.length ?? 0) === 0 && block.party_count > 0 && (
                <Badge variant="muted" className="ml-1.5">
                  totals only
                </Badge>
              )}
            </Button>
          )
        })}
      </div>
      <p className="text-xs text-muted-foreground">
        One block per denomination with an open item, ordered by open documents - amounts in two
        currencies are never added or compared. Nothing on this screen is converted.
      </p>
    </div>
  )
}
