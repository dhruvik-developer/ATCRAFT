/**
 * Job-work invoices: every bill raised against a job-work order.
 *
 * This is the same register the vendor-invoices screen shows, narrowed to
 * `invoice_type=JOB_WORK` through the list endpoint's filter. The order column
 * resolves the code from one bounded orders read, the way the orders panel
 * resolves vendor names - an order missing from that projection (a company
 * with more than two hundred of them) reads as a short id rather than a
 * blank.
 *
 * Recording happens here too. "Record job-work invoice" opens the dialog that
 * picks the vendor, the order and the receipt lines, and the row opens the
 * shared match dialog, which is where the bill is approved. Approval stays
 * there by design: whoever keys a bill must not clear it from the same
 * screen.
 */

import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Plus } from 'lucide-react'
import { Suspense, lazy, useMemo, useState } from 'react'

import { DataTable } from '@/components/data-table'
import { StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { financeKeys, type VendorInvoiceSummary } from '@/features/finance/finance-types'
import { VendorInvoiceMatchDialog } from '@/features/finance/vendor-invoice-match-dialog'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { formatDate, formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { jobWorkKeys, type JobWorkOrderRow } from './job-work-model'

const JobWorkInvoiceDialog = lazy(() =>
  import('./job-work-invoice-dialog').then((m) => ({ default: m.JobWorkInvoiceDialog })),
)

/** The order codes behind the invoice rows, in one bounded read. */
function useOrderCodes() {
  return useQuery({
    queryKey: [...jobWorkKeys.all, 'order-codes'] as const,
    queryFn: () =>
      api.getPage<JobWorkOrderRow>('/job-work', {
        params: { limit: LOOKUP_LIST_LIMIT, sort_by: 'created_at', sort_dir: 'desc' },
      }),
    staleTime: staleTimes.live,
  })
}

export default function JobWorkInvoicesTab() {
  const { can } = useAuth()
  const [openInvoiceId, setOpenInvoiceId] = useState<string | null>(null)
  const [recordOpen, setRecordOpen] = useState(false)

  // UX only; the API authorises the create and the submit itself.
  const canRecord = can(P.VENDOR_INVOICE_CREATE)

  const list = useListQuery<VendorInvoiceSummary>({
    path: '/finance/vendor-invoices',
    queryKey: financeKeys.vendorInvoices,
    defaultSortBy: 'invoice_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: { invoice_type: 'JOB_WORK' },
  })

  const ordersQuery = useOrderCodes()
  const orderById = useMemo(() => indexById(ordersQuery.data?.data ?? []), [ordersQuery.data])

  const columns = useMemo<ColumnDef<VendorInvoiceSummary, unknown>[]>(
    () => [
      {
        id: 'code',
        header: 'Invoice',
        cell: ({ row }) => (
          <div>
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                setOpenInvoiceId(row.original.id)
              }}
              className="font-medium hover:underline"
            >
              {row.original.vendor_invoice_number}
            </button>
            <span className="block font-mono text-2xs text-muted-foreground">
              {row.original.code}
            </span>
          </div>
        ),
      },
      {
        id: 'job_work_order',
        header: 'Order',
        enableSorting: false,
        cell: ({ row }) => {
          const order = row.original.job_work_order_id
            ? orderById.get(row.original.job_work_order_id)
            : undefined
          if (order) return <span className="font-medium">{order.code}</span>
          if (row.original.job_work_order_id) {
            return (
              <span className="font-mono text-xs" title={row.original.job_work_order_id}>
                {row.original.job_work_order_id.slice(0, 8)}
              </span>
            )
          }
          return <span className="text-muted-foreground">-</span>
        },
      },
      {
        accessorKey: 'invoice_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.invoice_date),
      },
      {
        accessorKey: 'total_amount',
        header: 'Total',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.total_amount, { currency: row.original.currency }),
      },
      {
        id: 'match_status',
        header: 'Match',
        enableSorting: false,
        cell: ({ row }) => (
          <Badge
            variant={
              row.original.match_status === 'MATCHED'
                ? 'success'
                : row.original.match_status === 'OVERRIDDEN'
                  ? 'warning'
                  : row.original.match_status === 'VARIANCE'
                    ? 'destructive'
                    : 'muted'
            }
          >
            {humanise(row.original.match_status)}
          </Badge>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [orderById],
  )

  const recordButton = canRecord ? (
    <Button onClick={() => setRecordOpen(true)}>
      <Plus />
      Record job-work invoice
    </Button>
  ) : null

  return (
    <div className="space-y-4">
      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search invoice number"
        getRowId={(row) => row.id}
        onRowClick={(row) => setOpenInvoiceId(row.id)}
        emptyTitle="No job-work invoices"
        emptyDescription="Nothing billed against a job-work order yet. Record one to bill an issued order's receipt lines."
        emptyAction={recordButton}
        toolbar={recordButton}
      />

      <VendorInvoiceMatchDialog invoiceId={openInvoiceId} onClose={() => setOpenInvoiceId(null)} />

      {recordOpen && (
        <Suspense fallback={null}>
          <JobWorkInvoiceDialog open onOpenChange={(next) => !next && setRecordOpen(false)} />
        </Suspense>
      )}
    </div>
  )
}
