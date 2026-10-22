/**
 * One job-work vendor's material account for a period.
 *
 * Opening, issued, accounted back (good output, unworked returns, process
 * loss) and closing per input item - beside the charges the period accrued,
 * what invoices have billed of those lines, and the excess-loss value debited.
 * The reconcilable identity on every row is closing = opening + issued -
 * consumed. Export is a client CSV, which the spec allows as the simpler
 * option beside the report-job pipeline.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Download } from 'lucide-react'
import { useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { Button } from '@/components/ui/button'
import { Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { formatDate, formatMoney, formatQuantity, toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { success } from '@/lib/messages'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import { useVendorLedger, type VendorLedgerItemRow } from './job-work-model'

function startOfMonth(): string {
  const now = new Date()
  return toDateInput(new Date(now.getFullYear(), now.getMonth(), 1))
}

function today(): string {
  return toDateInput(new Date())
}

/** The ledger as a CSV download, straight from the rows on screen. */
function downloadCsv(vendorCode: string, rows: VendorLedgerItemRow[]): void {
  const header = [
    'item',
    'opening',
    'issued',
    'consumed',
    'good_output',
    'returned',
    'scrap',
    'closing',
    'charges_accrued',
    'charges_billed',
    'vendor_debits',
  ]
  const escape = (value: string): string =>
    /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
  const body = rows.map((row) =>
    [
      row.item_code || row.item_id,
      row.opening_quantity,
      row.issued_quantity,
      row.input_consumed_quantity,
      row.received_output_quantity,
      row.returned_quantity,
      row.scrap_quantity,
      row.closing_quantity,
      row.charges_accrued,
      row.charges_billed,
      row.vendor_debits,
    ]
      .map((value) => escape(String(value)))
      .join(','),
  )
  const blob = new Blob([[header.join(','), ...body].join('\n')], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `vendor-ledger-${vendorCode}.csv`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export default function JobWorkLedgerTab() {
  const { can } = useAuth()
  const [vendorId, setVendorId] = useState<string | null>(null)
  const [dateFrom, setDateFrom] = useState(startOfMonth)
  const [dateTo, setDateTo] = useState(today)

  const ledger = useVendorLedger({ vendorId, dateFrom, dateTo })
  const rows = useMemo(() => ledger.data?.items ?? [], [ledger.data])

  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  const columns = useMemo<ColumnDef<VendorLedgerItemRow, unknown>[]>(
    () => [
      {
        accessorKey: 'item_code',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="font-mono text-xs">{row.original.item_code || '-'}</span>
        ),
      },
      {
        accessorKey: 'opening_quantity',
        header: 'Opening',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.opening_quantity)}</span>
        ),
      },
      {
        accessorKey: 'issued_quantity',
        header: 'Issued',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.issued_quantity)}</span>
        ),
      },
      {
        accessorKey: 'received_output_quantity',
        header: 'Good output',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.received_output_quantity)}</span>
        ),
      },
      {
        accessorKey: 'returned_quantity',
        header: 'Returned',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.returned_quantity)}</span>
        ),
      },
      {
        accessorKey: 'scrap_quantity',
        header: 'Scrap',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.scrap_quantity)}</span>
        ),
      },
      {
        accessorKey: 'closing_quantity',
        header: 'Closing',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular font-medium">
            {formatQuantity(row.original.closing_quantity)}
          </span>
        ),
      },
      {
        accessorKey: 'charges_accrued',
        header: 'Charges',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatMoney(row.original.charges_accrued)}</span>
        ),
      },
      {
        accessorKey: 'vendor_debits',
        header: 'Debits',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          Number(row.original.vendor_debits) > 0 ? (
            <span className="tabular font-medium text-destructive">
              {formatMoney(row.original.vendor_debits)}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
    ],
    [],
  )

  const vendor = vendorId ? vendorById.get(vendorId) : undefined

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <VendorPicker
          id="ledger-vendor"
          label="Vendor"
          value={vendorId}
          onChange={setVendorId}
          placeholder="Choose a vendor"
        />
        <div>
          <Label htmlFor="ledger-from">From</Label>
          <Input
            id="ledger-from"
            type="date"
            value={dateFrom}
            max={dateTo}
            onChange={(event) => setDateFrom(event.target.value)}
            className="mt-1.5"
          />
        </div>
        <div>
          <Label htmlFor="ledger-to">To</Label>
          <Input
            id="ledger-to"
            type="date"
            value={dateTo}
            min={dateFrom}
            onChange={(event) => setDateTo(event.target.value)}
            className="mt-1.5"
          />
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={rows.length === 0}
          onClick={() => {
            downloadCsv(vendor?.code ?? 'vendor', rows)
            toast.success(success.exported('Ledger'), `${rows.length} row(s) downloaded as CSV.`)
          }}
        >
          <Download />
          Export CSV
        </Button>
      </div>

      {!vendorId ? (
        <p className="text-sm text-muted-foreground">
          Choose a vendor to open their material account for the period {formatDate(dateFrom)} -{' '}
          {formatDate(dateTo)}.
        </p>
      ) : (
        <DataTable
          columns={columns}
          data={rows}
          isLoading={ledger.isLoading}
          isFetching={ledger.isFetching}
          error={ledger.error as Error | null}
          onRefresh={() => void ledger.refetch()}
          getRowId={(row) => row.item_id}
          emptyTitle="No movement in this period"
          emptyDescription="This vendor has no challans or receipts for these dates."
        />
      )}
    </div>
  )
}
