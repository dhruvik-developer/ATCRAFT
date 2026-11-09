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

import { describeError, salesInvoiceMessages } from '@/lib/messages'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroller,
} from '@/components/ui/table'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney, formatNumber, formatPercent, toDecimal } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import { cn } from '@/lib/utils'

import { financeKeys, type GstSection, type GstSummary } from './finance-types'
import { periodBounds } from './gst-period'

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
    queryFn: ({ signal }) =>
      api.get<GstSummary>('/finance/gst-summary', {
        params: { date_from: bounds?.from, date_to: bounds?.to },
        signal,
      }),
    staleTime: staleTimes.transactional,
    enabled: Boolean(bounds),
  })

  const data = summary.data
  // Read off a loaded statement only. `toDecimal(undefined)` is zero, so a
  // failed load would otherwise paint a green "credit carried forward" of
  // nothing - a filed-looking nil return where there is no return at all.
  const isCredit = data ? toDecimal(data.net_payable).lessThanOrEqualTo(0) : false

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
          <AlertDescription>{describeError(summary.error)}</AlertDescription>
        </Alert>
      )}

      <p className="text-sm text-muted-foreground">{salesInvoiceMessages.gst}</p>
      {data?.dispatched_not_invoiced && (
        <p className="text-sm">
          Dispatched not yet invoiced GST: {formatMoney(data.dispatched_not_invoiced.total_tax)}
        </p>
      )}
      <div className="grid gap-3 md:grid-cols-3">
        <StatTile
          label="Output tax"
          value={data ? formatMoney(data.output.total_tax) : '-'}
          hint={
            data
              ? `${formatNumber(data.output.rows?.length ?? 0)} rate slabs`
              : 'Tax charged on sales'
          }
          isLoading={summary.isLoading}
        />
        <StatTile
          label="Input tax credit"
          value={data ? formatMoney(data.input.total_tax) : '-'}
          hint={
            data
              ? `${formatNumber(data.input.rows?.length ?? 0)} rate slabs`
              : 'Credit on purchases'
          }
          isLoading={summary.isLoading}
        />
        <StatTile
          label={isCredit ? 'Credit carried forward' : 'Net tax payable'}
          value={data ? formatMoney(data.net_payable) : '-'}
          tone={!data ? 'default' : isCredit ? 'success' : 'warning'}
          isLoading={summary.isLoading}
        />
      </div>

      {/* On its own line rather than as the net tile's hint: StatTile shows a
          hint on one truncated line, and the three figures together are wider
          than a tile at every three-column width below xl. */}
      {data && (
        <p className="text-xs text-muted-foreground">
          Net split: CGST {formatMoney(data.net_cgst)} / SGST {formatMoney(data.net_sgst)} / IGST{' '}
          {formatMoney(data.net_igst)}
        </p>
      )}

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
            // Reachable only when the load failed or the period could not be
            // asked for, so the copy names the cause rather than asserting an
            // empty return.
            <p className="py-10 text-center text-sm text-muted-foreground">
              {summary.error
                ? 'The GST summary could not be loaded.'
                : !bounds
                  ? 'The period in the address is not a valid month. Choose a return period above.'
                  : 'No GST figures are available for this period.'}
            </p>
          ) : (
            <TableScroller label="Output and input tax by component">
              <Table>
                <caption className="sr-only">
                  Output and input tax by component from {formatDate(data.date_from)} to{' '}
                  {formatDate(data.date_to)}
                </caption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Component</TableHead>
                    <TableHead align="right">Output</TableHead>
                    <TableHead align="right">Input</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {COMPONENTS.map((component) => (
                    <TableRow key={component.key}>
                      <th
                        scope="row"
                        className={cn(
                          'px-3 py-2 text-left font-normal',
                          component.emphasis && 'font-medium',
                        )}
                      >
                        {component.label}
                      </th>
                      <TableCell align="right" className={cn(component.emphasis && 'font-medium')}>
                        {formatMoney(amountOf(data.output, component.key))}
                      </TableCell>
                      <TableCell align="right" className={cn(component.emphasis && 'font-medium')}>
                        {formatMoney(amountOf(data.input, component.key))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroller>
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
          <TableScroller label={title}>
            <Table>
              <caption className="sr-only">{title}</caption>
              <TableHeader>
                <TableRow>
                  <TableHead>HSN</TableHead>
                  <TableHead align="right">Rate</TableHead>
                  <TableHead align="right">Taxable</TableHead>
                  <TableHead align="right">CGST</TableHead>
                  <TableHead align="right">SGST</TableHead>
                  <TableHead align="right">IGST</TableHead>
                  <TableHead align="right">Documents</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={`${row.hsn_code ?? 'none'}-${row.tax_rate}`}>
                    <th scope="row" className="px-3 py-2 text-left font-mono text-xs font-normal">
                      {row.hsn_code ?? 'Unclassified'}
                    </th>
                    <TableCell align="right">{formatPercent(row.tax_rate)}</TableCell>
                    <TableCell align="right">{formatMoney(row.taxable_amount)}</TableCell>
                    <TableCell align="right">{formatMoney(row.cgst_amount)}</TableCell>
                    <TableCell align="right">{formatMoney(row.sgst_amount)}</TableCell>
                    <TableCell align="right">{formatMoney(row.igst_amount)}</TableCell>
                    <TableCell align="right" className="text-muted-foreground">
                      {formatNumber(row.document_count)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroller>
        )}
      </CardContent>
    </Card>
  )
}
