import type { ColumnDef } from '@tanstack/react-table'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useCategoryOptions } from '@/features/items/item-model'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatPercent } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

type InboundOrder = SchemaOf<'InboundTrackingRead'>
const flags = [
  ['overdue', 'Overdue'],
  ['expected_today', 'Expected today'],
  ['in_transit', 'In transit'],
  ['not_acknowledged', 'Not acknowledged'],
  ['include_completed', 'Include completed'],
] as const
const filterKeys = [...flags.map(([key]) => key), 'vendor_id', 'category_id']
const baseColumns: ColumnDef<InboundOrder, unknown>[] = [
  {
    accessorKey: 'code',
    header: 'Order',
    cell: ({ row }) => (
      <Link
        className="font-medium text-primary hover:underline"
        to={`/procurement/purchase-orders/${row.original.id}`}
      >
        {row.original.code}
      </Link>
    ),
  },
  { accessorKey: 'vendor_name', header: 'Vendor' },
  {
    accessorKey: 'item_category_name',
    header: 'Category',
    cell: ({ row }) => row.original.item_category_name ?? 'Several or none',
  },
  {
    id: 'dates',
    header: 'Delivery dates',
    cell: ({ row: { original: order } }) => (
      <div className="space-y-1 whitespace-nowrap text-xs">
        <p>Planning: {formatDate(order.expected_delivery_date)}</p>
        <p>Committed: {formatDate(order.vendor_committed_date)}</p>
        <p>Expected: {formatDate(order.delivery_expected_date)}</p>
        {order.days_late > 0 && <Badge variant="destructive">{order.days_late}d late</Badge>}
      </div>
    ),
  },
  {
    id: 'status',
    header: 'Status',
    cell: ({ row }) => (
      <div className="space-y-2">
        <StatusBadge status={row.original.status} />
        <StatusBadge status={row.original.tracking_status} />
      </div>
    ),
  },
  {
    id: 'shipment',
    header: 'Transport',
    cell: ({ row: { original: order } }) => (
      <div className="max-w-52 break-words text-sm">
        <p>{order.courier_name ?? 'Not dispatched'}</p>
        <p className="text-muted-foreground">{order.tracking_reference ?? '—'}</p>
        <p>{order.shipment_count} shipment(s)</p>
      </div>
    ),
  },
  {
    id: 'receiving',
    header: 'Receiving',
    cell: ({ row: { original: order } }) => (
      <div className="space-y-1 whitespace-nowrap text-sm">
        <p>{formatPercent(order.received_percentage)} received</p>
        {order.grn_pending && <Badge variant="secondary">GRN pending</Badge>}
        {order.qc_pending && <Badge variant="warning">QC pending</Badge>}
      </div>
    ),
  },
  {
    id: 'documents',
    header: 'Documents',
    cell: ({ row }) => (
      <span>
        {row.original.documents_count} file(s){row.original.has_lr_copy ? ' · LR attached' : ''}
      </span>
    ),
  },
]
const columns = baseColumns.map((column) => ({ ...column, enableSorting: false }))

export default function InboundTrackingPage() {
  const { can } = useAuth()
  const [params] = useSearchParams()
  const categories = useCategoryOptions(can(P.CATEGORY_VIEW))
  const vendorId = params.get('vendor_id') ?? ''
  const categoryId = params.get('category_id') ?? ''
  const list = useListQuery<InboundOrder>({
    path: '/procurement/inbound-tracking',
    queryKey: queryKeys.procurement.inboundTracking,
    staleTime: staleTimes.transactional,
    refetchOnWindowFocus: true,
    filters: {
      ...Object.fromEntries(
        flags.filter(([key]) => params.get(key) === 'true').map(([key]) => [key, true]),
      ),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(categoryId ? { category_id: categoryId } : {}),
    },
  })
  return (
    <>
      <PageHeader
        title="Inbound tracking"
        description="Follow vendor commitments, consignments, receiving and quality. Planning dates stay separate until a buyer adopts a commitment."
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
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search order, vendor or tracking reference…"
        getRowId={(row) => row.id}
        emptyTitle="No inbound orders found"
        emptyDescription="Clear a filter or include completed orders to see earlier deliveries."
        toolbar={
          <>
            {can(P.VENDOR_VIEW) && (
              <VendorPicker
                id="inbound-vendor"
                label="Vendor"
                hideLabel
                value={vendorId || null}
                onChange={(id) => list.setParam('vendor_id', id ?? undefined)}
                placeholder="All vendors"
              />
            )}
            {can(P.CATEGORY_VIEW) && (
              <select
                aria-label="Filter by category"
                className="h-10 rounded-md border bg-background px-3 text-sm"
                value={categoryId}
                onChange={(event) => list.setParam('category_id', event.target.value || undefined)}
              >
                <option value="">All categories</option>
                {categories.data?.map((category) => (
                  <option key={category.id} value={category.id}>
                    {category.name}
                  </option>
                ))}
              </select>
            )}
            {flags.map(([key, label]) => (
              <div className="flex min-h-11 items-center gap-2" key={key}>
                <Checkbox
                  id={`inbound-${key}`}
                  checked={params.get(key) === 'true'}
                  onCheckedChange={(checked) =>
                    list.setParam(key, checked === true ? 'true' : undefined)
                  }
                />
                <Label className="font-normal" htmlFor={`inbound-${key}`}>
                  {label}
                </Label>
              </div>
            ))}
            {filterKeys.some((key) => params.has(key)) && (
              <Button
                variant="ghost"
                onClick={() =>
                  list.setParams(Object.fromEntries(filterKeys.map((key) => [key, undefined])))
                }
              >
                Clear filters
              </Button>
            )}
          </>
        }
      />
    </>
  )
}
