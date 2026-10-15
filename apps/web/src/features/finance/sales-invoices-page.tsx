import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { SalesInvoiceDetailDialog } from './sales-invoice-dialogs'
import {
  SALES_INVOICE_STATUSES,
  salesInvoiceKeys,
  type SalesInvoiceSummary,
} from './sales-invoice-types'

export default function SalesInvoicesPage() {
  const { can } = useAuth()
  const [params] = useSearchParams()
  const [openId, setOpenId] = useState<string | null>(params.get('invoice_id'))
  const list = useListQuery<SalesInvoiceSummary>({
    path: '/finance/sales-invoices',
    queryKey: salesInvoiceKeys.list,
    defaultSortBy: 'invoice_date',
    defaultSortDir: 'desc',
    enabled: can(P.SALES_INVOICE_VIEW),
    filters: {
      status: params.get('status') || undefined,
      customer_id: params.get('customer_id') || undefined,
      sales_order_id: params.get('sales_order_id') || undefined,
      date_from: params.get('date_from') || undefined,
      date_to: params.get('date_to') || undefined,
      overdue_only: params.get('overdue_only') === 'true',
    },
  })
  const columns = useMemo<ColumnDef<SalesInvoiceSummary, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Invoice',
        cell: ({ row }) => (
          <button
            className="font-medium hover:underline"
            onClick={() => setOpenId(row.original.id)}
          >
            {row.original.code}
          </button>
        ),
      },
      {
        accessorKey: 'invoice_date',
        header: 'Date',
        cell: ({ row }) => formatDate(row.original.invoice_date),
      },
      {
        id: 'buyer',
        header: 'Customer',
        enableSorting: false,
        cell: ({ row }) => row.original.buyer_name,
      },
      {
        accessorKey: 'total_amount',
        header: 'Total',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.total_amount, { currency: row.original.currency }),
      },
      {
        accessorKey: 'paid_amount',
        header: 'Paid',
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.paid_amount, { currency: row.original.currency }),
      },
      {
        id: 'balance',
        header: 'Balance',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          formatMoney(row.original.balance_amount, { currency: row.original.currency }),
      },
      {
        accessorKey: 'due_date',
        header: 'Due date',
        cell: ({ row }) => formatDate(row.original.due_date),
      },
      {
        id: 'overdue',
        header: 'Overdue days',
        enableSorting: false,
        cell: ({ row }) => row.original.overdue_days,
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
    ],
    [],
  )
  return (
    <>
      <PageHeader
        title="Sales invoices"
        description="Issued GST invoices, their receipt allocations and due dates."
        breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Sales invoices' }]}
      />
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
        searchValue={list.search}
        searchPlaceholder="Search invoice number or customer"
        onRefresh={() => void list.refetch()}
        onRowClick={(row) => setOpenId(row.id)}
        getRowId={(row) => row.id}
        emptyTitle="No sales invoices"
        emptyDescription="Raise an invoice from a ready sales dispatch."
        toolbar={
          <>
            <Select
              value={params.get('status') || SELECT_ALL}
              onValueChange={(next) =>
                list.setParam('status', next === SELECT_ALL ? undefined : next)
              }
            >
              <SelectTrigger aria-label="Invoice status" className="w-full sm:w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any status</SelectItem>
                {SALES_INVOICE_STATUSES.map((state) => (
                  <SelectItem key={state} value={state}>
                    {humanise(state)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="w-full sm:w-60">
              <CustomerPicker
                id="sales-invoice-customer"
                label="Customer"
                value={params.get('customer_id')}
                onChange={(next) => list.setParam('customer_id', next || undefined)}
              />
            </div>
            <Input
              aria-label="Invoice date from"
              type="date"
              value={params.get('date_from') || ''}
              onChange={(event) => list.setParam('date_from', event.target.value || undefined)}
              className="w-full sm:w-40"
            />
            <Input
              aria-label="Invoice date to"
              type="date"
              value={params.get('date_to') || ''}
              onChange={(event) => list.setParam('date_to', event.target.value || undefined)}
              className="w-full sm:w-40"
            />
            <Label className="flex items-center gap-2">
              <Checkbox
                checked={params.get('overdue_only') === 'true'}
                onCheckedChange={(checked) =>
                  list.setParam('overdue_only', checked ? 'true' : undefined)
                }
              />
              Overdue only
            </Label>
          </>
        }
      />
      {can(P.SALES_INVOICE_VIEW) && (
        <SalesInvoiceDetailDialog invoiceId={openId} onClose={() => setOpenId(null)} />
      )}
    </>
  )
}
