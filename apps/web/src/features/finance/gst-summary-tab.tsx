/**
 * GST summary for one return period.
 *
 * Output tax, input credit and the net position all come from the API. Nothing
 * is added up here: a GST figure the browser derived and the return the
 * accountant files must never be two different numbers (master rules 84).
 *
 * The endpoint takes an explicit `date_from`/`date_to` pair, both required. The
 * control is still a month picker, because a return is filed for a month and
 * asking an accountant for two dates that must be the first and last of one is
 * a worse question; the month is expanded to its bounds on the way out.
 *
 * The slab tables are the API's own `rows`: one line per HSN code and rate,
 * which is the grain a GSTR return is actually filed at. `document_count` is
 * per slab and is NOT summed into a section total - one invoice can appear in
 * several slabs, so adding them would over-count documents.
 */

import { useQuery } from '@tanstack/react-query'
import { format } from 'date-fns'
import { RefreshCw } from 'lucide-react'

import { StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney, formatNumber, formatPercent, toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { financeKeys, type GstSection, type GstSummary } from './finance-types'

const COMPONENTS: { key: keyof GstSection; label: string; emphasis?: boolean }[] = [
  { key: 'taxable_amount', label: 'Taxable value' },
  { key: 'cgst_amount', label: 'CGST' },
  { key: 'sgst_amount', label: 'SGST' },
  { key: 'igst_amount', label: 'IGST' },
  { key: 'total_tax', label: 'Total tax', emphasis: true },
]

/** `rows` is the only non-money member, and it is never read through here. */
function amountOf(section: GstSection | undefined, key: keyof GstSection): string {
  const value = section?.[key]
  return typeof value === 'string' ? value : '0'
}

/** `YYYY-MM`, with the month constrained so a stray URL cannot ask for month 13. */
const PERIOD_PATTERN = /^(\d{4})-(0[1-9]|1[0-2])$/

/**
 * The first and last day of the month a return is filed for.
 *
 * Computed from the `YYYY-MM` text itself rather than through a local `Date`. A
 * return period is a calendar fact about a filing, not an instant: routing it
 * through a `Date` puts the browser's clock and zone - and the parser's habit of
 * filling unspecified units from a reference date - between the month the
 * accountant chose and the dates the API is asked for. Two strings in, two
 * strings out, and the same bounds on every machine.
 */
function periodBounds(period: string): { from: string; to: string } | null {
  const match = PERIOD_PATTERN.exec(period)
  if (!match) return null
  const [, year, month] = match
  // Day zero of the following month is the last day of this one, and `Date.UTC`
  // keeps even that one piece of arithmetic out of the local zone.
  const lastDay = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate()
  return {
    from: `${year}-${month}-01`,
    to: `${year}-${month}-${String(lastDay).padStart(2, '0')}`,
  }
}

export function GstSummaryTab({
  period,
  onPeriodChange,
}: {
  /** `YYYY-MM`, the shape a GST return is filed in. */
  period: string
  onPeriodChange: (next: string) => void
}) {
  const bounds = periodBounds(period)

  const summary = useQuery({
    queryKey: financeKeys.gstSummary(bounds?.from ?? '', bounds?.to ?? ''),
    queryFn: () =>
      api.get<GstSummary>('/finance/gst-summary', {
        params: { date_from: bounds?.from, date_to: bounds?.to },
      }),
    staleTime: staleTimes.transactional,
    enabled: Boolean(bounds),
  })

  const data = summary.data
  const net = toDecimal(data?.net_payable)
  const isCredit = net.lessThanOrEqualTo(0)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="gst-period" className="text-xs">
            Return period
          </Label>
          <Input
            id="gst-period"
            type="month"
            className="w-[11rem]"
            value={period}
            max={format(new Date(), 'yyyy-MM')}
            onChange={(event) => onPeriodChange(event.target.value)}
          />
        </div>

        <div className="flex items-center gap-3">
          {data && (
            <span className="text-xs text-muted-foreground">
              Covering {formatDate(data.date_from)} to {formatDate(data.date_to)}
            </span>
          )}
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => void summary.refetch()}
            aria-label="Refresh GST summary"
          >
            <RefreshCw className={summary.isFetching ? 'animate-spin' : undefined} />
          </Button>
        </div>
      </div>

      {summary.error && !summary.isLoading && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the GST summary</AlertTitle>
          <AlertDescription>{summary.error.message}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Output tax"
          value={formatMoney(data?.output.total_tax)}
          hint={
            data
              ? `${formatNumber(data.output.rows?.length ?? 0)} rate slabs`
              : 'Tax charged on sales'
          }
          isLoading={summary.isLoading}
        />
        <StatTile
          label="Input tax credit"
          value={formatMoney(data?.input.total_tax)}
          hint={
            data
              ? `${formatNumber(data.input.rows?.length ?? 0)} rate slabs`
              : 'Credit on purchases'
          }
          isLoading={summary.isLoading}
        />
        <StatTile
          label={isCredit ? 'Credit carried forward' : 'Net tax payable'}
          value={formatMoney(data?.net_payable)}
          hint={
            data
              ? `CGST ${formatMoney(data.net_cgst)} / SGST ${formatMoney(
                  data.net_sgst,
                )} / IGST ${formatMoney(data.net_igst)}`
              : undefined
          }
          tone={isCredit ? 'success' : 'warning'}
          isLoading={summary.isLoading}
        />
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Components</CardTitle>
          <p className="text-xs text-muted-foreground">
            As reported by the API for {period || 'the selected period'}.
          </p>
        </CardHeader>
        <CardContent>
          {summary.isLoading ? (
            <div className="space-y-2">
              {COMPONENTS.map((component) => (
                <Skeleton key={component.key} className="h-8 w-full" />
              ))}
            </div>
          ) : !data ? (
            <p className="py-10 text-center text-sm text-muted-foreground">
              No GST figures are available for this period.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">
                  Output and input tax by component from {formatDate(data.date_from)} to{' '}
                  {formatDate(data.date_to)}
                </caption>
                <thead>
                  <tr className="border-b text-xs uppercase tracking-wide text-muted-foreground">
                    <th scope="col" className="py-2 text-left font-semibold">
                      Component
                    </th>
                    <th scope="col" className="py-2 text-right font-semibold">
                      Output
                    </th>
                    <th scope="col" className="py-2 text-right font-semibold">
                      Input
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {COMPONENTS.map((component) => (
                    <tr key={component.key} className="border-b last:border-0">
                      <th
                        scope="row"
                        className={cn(
                          'py-2 text-left font-normal',
                          component.emphasis && 'font-medium',
                        )}
                      >
                        {component.label}
                      </th>
                      <td
                        className={cn(
                          'py-2 text-right tabular',
                          component.emphasis && 'font-medium',
                        )}
                      >
                        {formatMoney(amountOf(data.output, component.key))}
                      </td>
                      <td
                        className={cn(
                          'py-2 text-right tabular',
                          component.emphasis && 'font-medium',
                        )}
                      >
                        {formatMoney(amountOf(data.input, component.key))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {data && (
        <div className="grid gap-4 xl:grid-cols-2">
          <SlabTable title="Output tax by HSN and rate" section={data.output} />
          <SlabTable title="Input credit by HSN and rate" section={data.input} />
        </div>
      )}
    </div>
  )
}

/**
 * One section at the grain the return is filed at.
 *
 * The document count belongs to the slab, not to the section: a single invoice
 * with two rates appears under both, so these are deliberately not added up.
 */
function SlabTable({ title, section }: { title: string; section: GstSection }) {
  // The contract declares `rows` optional - it defaults to an empty list on the
  // server. A missing list must render an empty slab table, never a crash.
  const rows = section.rows ?? []

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
        <p className="text-xs text-muted-foreground">
          One line per HSN code and rate. Document counts are per slab and are not summed - an
          invoice spanning two rates is counted in each.
        </p>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            Nothing was reported in this period.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">{title}</caption>
              <thead>
                <tr className="border-b text-xs uppercase tracking-wide text-muted-foreground">
                  <th scope="col" className="py-2 text-left font-semibold">
                    HSN
                  </th>
                  <th scope="col" className="py-2 text-right font-semibold">
                    Rate
                  </th>
                  <th scope="col" className="py-2 text-right font-semibold">
                    Taxable
                  </th>
                  <th scope="col" className="py-2 text-right font-semibold">
                    CGST
                  </th>
                  <th scope="col" className="py-2 text-right font-semibold">
                    SGST
                  </th>
                  <th scope="col" className="py-2 text-right font-semibold">
                    IGST
                  </th>
                  <th scope="col" className="py-2 text-right font-semibold">
                    Documents
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={`${row.hsn_code ?? 'none'}-${row.tax_rate}`}
                    className="border-b last:border-0"
                  >
                    <th scope="row" className="py-2 text-left font-mono text-xs font-normal">
                      {row.hsn_code ?? 'Unclassified'}
                    </th>
                    <td className="py-2 text-right tabular">{formatPercent(row.tax_rate)}</td>
                    <td className="py-2 text-right tabular">{formatMoney(row.taxable_amount)}</td>
                    <td className="py-2 text-right tabular">{formatMoney(row.cgst_amount)}</td>
                    <td className="py-2 text-right tabular">{formatMoney(row.sgst_amount)}</td>
                    <td className="py-2 text-right tabular">{formatMoney(row.igst_amount)}</td>
                    <td className="py-2 text-right tabular text-muted-foreground">
                      {formatNumber(row.document_count)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
