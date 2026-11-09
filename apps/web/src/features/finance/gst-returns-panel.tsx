/**
 * GSTR-3B preparation summary and the GSTR-1 workbook export.
 *
 * Both requests use the same return-month bounds as the GST summary above.
 * The figures are preparation aids from posted documents, not a filing
 * integration; the workbook job remains traceable through the normal report
 * status and download flow.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { FileSpreadsheet } from 'lucide-react'

import { LoadError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import { formatMoney, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { financeKeys, type Gstr3b } from './finance-types'
import { periodBounds } from './gst-period'

function AmountRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b py-2 last:border-0">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-right text-sm tabular-nums">{formatMoney(value)}</span>
    </div>
  )
}

export function GstReturnsPanel({ period }: { period: string }) {
  const { can } = useAuth()
  const bounds = periodBounds(period)
  const canExport = can(P.REPORT_EXPORT)
  const job = useReportJob()

  const summary = useQuery({
    queryKey: financeKeys.gstr3b(bounds?.from ?? '', bounds?.to ?? ''),
    queryFn: () =>
      api.get<Gstr3b>('/finance/gstr-3b', {
        params: { date_from: bounds?.from, date_to: bounds?.to },
      }),
    enabled: Boolean(bounds),
    staleTime: staleTimes.transactional,
  })

  const queueExport = useMutation({
    mutationFn: () =>
      api.post<AcceptedJobData>('/finance/gstr-1/export', undefined, {
        params: { date_from: bounds?.from, date_to: bounds?.to },
      }),
    onSuccess: (accepted) => job.start(accepted),
  })

  const data = summary.data
  const net = data?.net_tax_payable

  return (
    <section
      className="space-y-4 rounded-lg border bg-card p-4"
      aria-labelledby="gst-returns-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="gst-returns-title" className="text-base font-semibold">
            GST return preparation
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Posted documents for {period || 'the selected return period'}; preparation aids only.
          </p>
        </div>
        {canExport && (
          <Button
            variant="outline"
            onClick={() => queueExport.mutate()}
            loading={queueExport.isPending}
            disabled={!bounds}
          >
            <FileSpreadsheet />
            Export GSTR-1 XLSX
          </Button>
        )}
      </div>

      {summary.isError && (
        <LoadError
          title="Could not load GSTR-3B"
          error={summary.error}
          onRetry={() => void summary.refetch()}
        />
      )}

      {summary.isError ? null : summary.isPending ? (
        <div className="grid gap-4 md:grid-cols-3">
          {['outward', 'itc', 'net'].map((tile) => (
            <div key={tile} className="h-36 animate-pulse rounded-md bg-muted" />
          ))}
        </div>
      ) : data ? (
        <div className="grid gap-4 md:grid-cols-3">
          <div className="rounded-md border p-3">
            <h3 className="mb-2 text-sm font-semibold">3.1 Outward supplies, net</h3>
            <AmountRow label="Taxable value" value={data.outward.net.taxable_amount} />
            <AmountRow label="CGST" value={data.outward.net.cgst_amount} />
            <AmountRow label="SGST" value={data.outward.net.sgst_amount} />
            <AmountRow label="IGST" value={data.outward.net.igst_amount} />
            <AmountRow label="Total tax" value={data.outward.net.tax_amount} />
          </div>
          <div className="rounded-md border p-3">
            <h3 className="mb-2 text-sm font-semibold">4 Input tax credit, net</h3>
            <AmountRow label="Taxable value" value={data.itc.net.taxable_amount} />
            <AmountRow label="CGST" value={data.itc.net.cgst_amount} />
            <AmountRow label="SGST" value={data.itc.net.sgst_amount} />
            <AmountRow label="IGST" value={data.itc.net.igst_amount} />
            <AmountRow label="Total tax" value={data.itc.net.tax_amount} />
            {!toDecimal(data.itc.net.unallocated_tax_amount).isZero() && (
              <AmountRow
                label="Debit-note tax without a tax-head split"
                value={data.itc.net.unallocated_tax_amount}
              />
            )}
          </div>
          <div className="rounded-md border p-3">
            <h3 className="mb-2 text-sm font-semibold">Net position</h3>
            <AmountRow label="CGST payable / (credit)" value={data.net_cgst} />
            <AmountRow label="SGST payable / (credit)" value={data.net_sgst} />
            <AmountRow label="IGST payable / (credit)" value={data.net_igst} />
            <AmountRow label="Tax payable / (credit)" value={net ?? '0'} />
            <p className="mt-2 text-xs text-muted-foreground">
              Prepared from issued sales invoices and credit notes, and approved vendor invoices
              less posted debit notes. Verify the return before filing.
            </p>
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Choose a return month to load GSTR-3B.</p>
      )}

      <ReportJobPanel controller={job} label="GSTR-1 workbook" />
    </section>
  )
}
