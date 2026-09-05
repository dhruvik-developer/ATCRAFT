/**
 * Open payables - every vendor invoice that still owes money.
 *
 * `GET /finance/outstanding` reads the vendor invoice register, so it is one
 * side of the ledger, not both: it takes `vendor_id`, never a `party_type`.
 * Receivables have no invoice document in this schema and are reached through
 * the Ageing tab, which nets them out of the ledger. The screen says so rather
 * than offering a "Receivables" switch the endpoint would silently ignore.
 *
 * Vendor, overdue-only, the report date and paging are all query parameters the
 * API resolves - an open-item list in a company with a long supply base is
 * exactly the table you must never pull into the browser to filter (master
 * rules 31/36/78).
 *
 * Every row states its own currency and is formatted in it. Nothing here adds
 * two denominations together: there is no exchange rate in this contract
 * (docs/assumptions.md section 27), so a column total would be meaningless and
 * none is shown.
 */

import type { ColumnDef } from '@tanstack/react-table'
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { StatusBadge } from '@/components/shared'
import { Badge, Checkbox, Input, Label } from '@/components/ui/primitives'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatMoney, toDateInput } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'

import { financeKeys, type OutstandingRow } from './finance-types'

/** The bucket the API assigns. `NOT_DUE` is inside terms, not late. */
const BUCKET_VARIANT: Record<string, 'success' | 'warning' | 'destructive' | 'muted'> = {
  NOT_DUE: 'muted',
  '0-30': 'success',
  '31-60': 'warning',
  '61-90': 'warning',
  '90+': 'destructive',
}

export function OutstandingTab() {
  const [searchParams] = useSearchParams()

  const vendorId = searchParams.get('vendor_id') ?? ''
  const overdueOnly = searchParams.get('overdue_only') === 'true'
  const asOf = searchParams.get('as_of') ?? ''

  const list = useListQuery<OutstandingRow>({
    path: '/finance/outstanding',
    queryKey: financeKeys.outstanding,
    // The endpoint sorts oldest due first and takes no sort or search
    // parameter, so neither control is offered - a sort box that the server
    // ignores is worse than none.
    // Receipts and payments post all day; a stale open-item list sends someone
    // to chase money that arrived this morning.
    staleTime: staleTimes.transactional,
    filters: {
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(overdueOnly ? { overdue_only: true } : {}),
      ...(asOf ? { as_of: asOf } : {}),
    },
  })

  const { setParam } = list

  const columns = useMemo<ColumnDef<OutstandingRow, unknown>[]>(
    () => [
      {
        id: 'vendor_name',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="max-w-[18rem]">
            <span className="block truncate font-medium">{row.original.vendor_name}</span>
            {row.original.vendor_code && (
              <span className="block truncate font-mono text-2xs text-muted-foreground">
                {row.original.vendor_code}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'code',
        header: 'Invoice',
        enableSorting: false,
        cell: ({ row }) => (
          <div>
            <span className="block font-medium">{row.original.vendor_invoice_number}</span>
            <span className="block font-mono text-2xs text-muted-foreground">
              {row.original.code}
            </span>
          </div>
        ),
      },
      {
        id: 'invoice_date',
        header: 'Date',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.invoice_date),
      },
      {
        id: 'due_date',
        header: 'Due',
        enableSorting: false,
        cell: ({ row }) => {
          // Days overdue and the bucket are both computed by the API against
          // its own clock and the report date, never re-derived here.
          const overdue = row.original.days_overdue
          return (
            <div className="flex items-center gap-2">
              <span>{formatDate(row.original.due_date)}</span>
              {overdue > 0 && (
                <Badge variant={overdue > 60 ? 'destructive' : 'warning'}>{overdue}d late</Badge>
              )}
            </div>
          )
        },
      },
      {
        id: 'ageing_bucket',
        header: 'Age',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge variant={BUCKET_VARIANT[row.original.ageing_bucket] ?? 'muted'}>
            {row.original.ageing_bucket === 'NOT_DUE'
              ? 'Not due'
              : `${row.original.ageing_bucket} days`}
          </Badge>
        ),
      },
      {
        id: 'currency',
        header: 'Currency',
        enableSorting: false,
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.currency}</span>,
      },
      {
        id: 'total_amount',
        header: 'Invoiced',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.total_amount, { currency: row.original.currency }),
      },
      {
        id: 'paid_amount',
        header: 'Settled',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {formatMoney(row.original.paid_amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'outstanding_amount',
        header: 'Outstanding',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.outstanding_amount, { currency: row.original.currency })}
          </span>
        ),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [],
  )

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Open supplier bills, oldest due first. Amounts are shown in the currency each invoice was
        raised in and are never converted or totalled across denominations. For what customers owe,
        use the Ageing tab.
      </p>

      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onRefresh={() => void list.refetch()}
        getRowId={(row) => row.invoice_id}
        emptyTitle="Nothing is owed by you"
        emptyDescription="No open supplier invoice matches these filters."
        toolbar={
          <>
            <VendorPicker
              id="outstanding-vendor"
              label="Vendor"
              hideLabel
              value={vendorId || null}
              onChange={(next) => setParam('vendor_id', next ?? undefined)}
            />

            <div className="flex items-center gap-2">
              <Label htmlFor="outstanding-as-of" className="text-xs text-muted-foreground">
                As on
              </Label>
              <Input
                id="outstanding-as-of"
                type="date"
                className="w-[11rem]"
                value={asOf}
                max={toDateInput(new Date())}
                onChange={(event) => setParam('as_of', event.target.value || undefined)}
              />
            </div>

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="outstanding-overdue"
                checked={overdueOnly}
                onCheckedChange={(checked) =>
                  setParam('overdue_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="outstanding-overdue" className="cursor-pointer text-xs font-normal">
                Overdue only
              </Label>
            </div>
          </>
        }
      />
    </div>
  )
}
