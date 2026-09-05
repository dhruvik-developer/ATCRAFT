/**
 * Purchase order list.
 *
 * Status, vendor, the expected-delivery window, "open only" and "overdue only"
 * are all query parameters the API answers by those exact names. An order book
 * of this size is never pulled into the browser to be filtered there (master
 * rules 31/36/78).
 *
 * The delay indicator is the reason most people open this screen: an order past
 * its expected delivery that is still open is money committed and material
 * missing, and it is called out in destructive weight rather than left for the
 * reader to work out from two date columns. Note that `PurchaseOrderSummary`
 * carries no received quantity, so "still open" is read from the status - the
 * one thing the row actually states - rather than from a receipt percentage the
 * API does not send.
 *
 * The supplier's name is not on the row either: the summary carries `vendor_id`
 * alone, so it is resolved through the bounded `/vendors/options` projection
 * the API publishes for this purpose.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Plus, TriangleAlert, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { DateRangeFilter, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Badge, Checkbox, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { daysUntil, formatDate, formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { PurchaseOrderFormDialog } from './purchase-order-form-dialog'
import {
  OPEN_PO_STATUSES,
  PURCHASE_ORDER_STATUSES,
  clearSearchKeys,
  isUrgentPriority,
  type PurchaseOrderRow,
} from './procurement-types'

/** Every filter this screen owns, cleared as one write rather than six. */
const FILTER_KEYS = [
  'status',
  'vendor_id',
  'expected_from',
  'expected_to',
  'open_only',
  'overdue_only',
] as const

/** An order still expecting a delivery, which is the API's `open_only` set. */
const OPEN_STATUSES = new Set<string>(OPEN_PO_STATUSES)

export default function PurchaseOrdersPage() {
  const { can } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  const status = searchParams.get('status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const expectedFrom = searchParams.get('expected_from') ?? ''
  const expectedTo = searchParams.get('expected_to') ?? ''
  const openOnly = searchParams.get('open_only') === 'true'
  const overdueOnly = searchParams.get('overdue_only') === 'true'

  const list = useListQuery<PurchaseOrderRow>({
    path: '/procurement/purchase-orders',
    queryKey: queryKeys.procurement.purchaseOrders,
    defaultSortBy: 'po_date',
    defaultSortDir: 'desc',
    // Orders move through approval and receipt all day; a stale list sends a
    // buyer to chase a delivery that landed an hour ago.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(expectedFrom ? { expected_from: expectedFrom } : {}),
      ...(expectedTo ? { expected_to: expectedTo } : {}),
      ...(openOnly ? { open_only: true } : {}),
      ...(overdueOnly ? { overdue_only: true } : {}),
    },
  })

  // Names for the vendor column. One bounded reference query, shared with every
  // other screen that needs it, rather than a lookup per row.
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])

  const { setParam } = list
  const filterCount =
    (status ? 1 : 0) +
    (vendorId ? 1 : 0) +
    (expectedFrom ? 1 : 0) +
    (expectedTo ? 1 : 0) +
    (openOnly ? 1 : 0) +
    (overdueOnly ? 1 : 0)

  const columns = useMemo<ColumnDef<PurchaseOrderRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Order',
        // A real link, not only a clickable row: this screen is driven from the
        // keyboard all day.
        cell: ({ row }) => (
          <Link
            to={`/procurement/purchase-orders/${row.original.id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.original.code}
          </Link>
        ),
      },
      {
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const vendor = vendorById.get(row.original.vendor_id)
          if (!vendor) {
            // A suspended vendor is not in the picker projection. A dash is
            // honest here; the order's own screen names the supplier.
            return <span className="text-muted-foreground">-</span>
          }
          return (
            <div className="max-w-[18rem]">
              <span className="block truncate">{vendor.name}</span>
              <span className="block truncate text-xs text-muted-foreground">{vendor.code}</span>
            </div>
          )
        },
      },
      {
        accessorKey: 'po_date',
        header: 'PO date',
        cell: ({ row }) => formatDate(row.original.po_date),
      },
      {
        accessorKey: 'expected_delivery_date',
        header: 'Expected',
        cell: ({ row }) => formatDate(row.original.expected_delivery_date),
      },
      {
        id: 'delay',
        header: 'Delay',
        enableSorting: false,
        cell: ({ row }) => {
          const days = daysUntil(row.original.expected_delivery_date)
          // Open is the API's own definition (OPEN_PO_STATUSES). The row states
          // no quantities, so a receipt percentage cannot be computed from it.
          const outstanding = OPEN_STATUSES.has(row.original.status)
          if (days === null || days >= 0 || !outstanding) {
            return <span className="text-muted-foreground">-</span>
          }
          return (
            <Badge variant="destructive">
              <TriangleAlert className="h-3 w-3" aria-hidden />
              {Math.abs(days)}d late
            </Badge>
          )
        },
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        id: 'source',
        header: 'Raised from',
        enableSorting: false,
        // Whether a buyer typed this or the planner produced it changes how the
        // quantity should be read, and the row states it already.
        cell: ({ row }) =>
          row.original.source_type === 'MANUAL' ? (
            <span className="text-xs text-muted-foreground">Manual</span>
          ) : (
            <Badge variant="secondary">{humanise(row.original.source_type)}</Badge>
          ),
      },
      {
        accessorKey: 'priority',
        header: 'Priority',
        meta: { align: 'right' },
        // 1-9, lower is more urgent. Only the urgent end is coloured; marking
        // every routine order would make the colour mean nothing.
        cell: ({ row }) => (
          <span
            className={
              isUrgentPriority(row.original.priority)
                ? 'tabular font-medium text-warning'
                : 'tabular text-muted-foreground'
            }
          >
            {row.original.priority}
          </span>
        ),
      },
      {
        accessorKey: 'total_amount',
        header: 'Total',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="font-medium">
            {formatMoney(row.original.total_amount, { currency: row.original.currency })}
          </span>
        ),
      },
    ],
    [vendorById],
  )

  const newOrderButton = can(P.PURCHASE_ORDER_CREATE) ? (
    <Button onClick={() => setCreateOpen(true)}>
      <Plus />
      New purchase order
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Purchase orders"
        description="What is on order and where it has got to. An order past its expected delivery and not fully received is flagged as delayed."
        actions={newOrderButton}
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
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search order code, vendor reference or tracking..."
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/procurement/purchase-orders/${row.id}`)}
        emptyTitle="No purchase orders found"
        emptyDescription="Nothing matches the current filters. Widen the date window or clear a filter."
        emptyAction={newOrderButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[190px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {PURCHASE_ORDER_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* The vendor search reads `/vendors`, which enforces `vendor.view`.
                Offering it without that right earns a 403 on every keystroke. */}
            {can(P.VENDOR_VIEW) && (
              <VendorPicker
                id="po-vendor-filter"
                className="w-full sm:w-auto"
                label="Vendor"
                hideLabel
                value={vendorId || null}
                onChange={(value) => setParam('vendor_id', value ?? undefined)}
                placeholder="All vendors"
              />
            )}

            {/* The window is on the expected delivery date, which is what the
                API filters on - `expected_from` / `expected_to`. */}
            <DateRangeFilter
              id="po-expected"
              fromLabel="Expected from"
              from={expectedFrom}
              to={expectedTo}
              onFromChange={(value) => setParam('expected_from', value || undefined)}
              onToChange={(value) => setParam('expected_to', value || undefined)}
            />

            <div className="flex items-center gap-2">
              <Checkbox
                id="po-open-only"
                checked={openOnly}
                onCheckedChange={(checked) =>
                  setParam('open_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="po-open-only" className="font-normal">
                Still expecting delivery
              </Label>
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="po-overdue-only"
                checked={overdueOnly}
                onCheckedChange={(checked) =>
                  setParam('overdue_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="po-overdue-only" className="font-normal">
                Overdue only
              </Label>
            </div>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => clearSearchKeys(setSearchParams, FILTER_KEYS)}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {can(P.PURCHASE_ORDER_CREATE) && (
        <PurchaseOrderFormDialog open={createOpen} onOpenChange={setCreateOpen} />
      )}
    </>
  )
}
