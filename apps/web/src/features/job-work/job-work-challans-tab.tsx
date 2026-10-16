/**
 * Company-wide delivery-challan register.
 *
 * The per-order challans answer "what travelled under this order"; this tab
 * answers "what left the factory, for whom, and when" - the register the GST
 * delivery challan is filed from. Each row links back to its order and offers
 * the Rule 55 PDF.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { Button } from '@/components/ui/button'
import { Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatMoney, formatQuantity } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { ChallanDownload } from './job-work-challan-download'
import { clearSearchKeys, jobWorkKeys, type ChallanRow } from './job-work-model'

const FILTER_KEYS = ['vendor_id', 'date_from', 'date_to'] as const

export default function JobWorkChallansTab() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  const vendorId = searchParams.get('vendor_id') ?? ''
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''

  const list = useListQuery<ChallanRow>({
    path: '/job-work/challans',
    queryKey: jobWorkKeys.challans,
    defaultSortBy: 'challan_date',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
    },
  })

  const { setParam } = list
  const filterCount = (vendorId ? 1 : 0) + (dateFrom ? 1 : 0) + (dateTo ? 1 : 0)

  const columns = useMemo<ColumnDef<ChallanRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Challan',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'order',
        header: 'Order',
        enableSorting: false,
        cell: ({ row }) => (
          <Link
            to={`/job-work/${row.original.job_work_order_id}`}
            className="hover:underline"
            title="Open this order"
          >
            {row.original.job_work_order_id.slice(0, 8)}
          </Link>
        ),
      },
      {
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const vendor = vendorById.get(row.original.vendor_id)
          return vendor ? (
            <span title={vendor.name}>{vendor.code}</span>
          ) : (
            <span className="text-muted-foreground">-</span>
          )
        },
      },
      {
        accessorKey: 'challan_date',
        header: 'Despatched',
        cell: ({ row }) => formatDate(row.original.challan_date),
      },
      {
        accessorKey: 'expected_return_date',
        header: 'Due back',
        enableSorting: false,
        cell: ({ row }) => formatDate(row.original.expected_return_date),
      },
      {
        id: 'quantity',
        header: 'Quantity',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatQuantity(row.original.total_quantity)}</span>
        ),
      },
      {
        id: 'value',
        header: 'Declared value',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => <span className="tabular">{formatMoney(row.original.total_value)}</span>,
      },
      {
        id: 'pdf',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <ChallanDownload challanId={row.original.id} challanCode={row.original.code} />
        ),
      },
    ],
    [vendorById],
  )

  return (
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
      searchPlaceholder="Search challan code or vehicle..."
      getRowId={(row) => row.id}
      emptyTitle="No challans found"
      emptyDescription="Nothing matches the current filters. Clear a filter to see the rest."
      toolbar={
        <>
          <VendorPicker
            id="challan-vendor-filter"
            label="Vendor"
            hideLabel
            value={vendorId || null}
            onChange={(value) => setParam('vendor_id', value ?? undefined)}
            placeholder="All vendors"
          />
          <div className="flex items-center gap-1.5">
            <Label htmlFor="challan-date-from" className="sr-only">
              Despatched from
            </Label>
            <Input
              id="challan-date-from"
              type="date"
              value={dateFrom}
              onChange={(event) => setParam('date_from', event.target.value || undefined)}
              aria-label="Despatched from"
            />
            <span className="text-muted-foreground" aria-hidden>
              -
            </span>
            <Label htmlFor="challan-date-to" className="sr-only">
              Despatched to
            </Label>
            <Input
              id="challan-date-to"
              type="date"
              value={dateTo}
              onChange={(event) => setParam('date_to', event.target.value || undefined)}
              aria-label="Despatched to"
            />
          </div>
          {filterCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => clearSearchKeys(setSearchParams, FILTER_KEYS)}
            >
              Clear
            </Button>
          )}
        </>
      }
    />
  )
}
