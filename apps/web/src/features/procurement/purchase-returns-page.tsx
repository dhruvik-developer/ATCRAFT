/**
 * Purchase returns: material going back to the vendor.
 *
 * The fact this list leads with is whether the goods have actually left. A
 * posted goods receipt cannot be retracted - an append-only ledger is corrected
 * by an offsetting entry - so a return is the only way material that was booked
 * in comes back out, and a return that has been raised but not posted is stock
 * the vendor thinks is coming back while it is still on the shelf.
 *
 * The two terminal states are reported separately and never collapsed into one
 * "closed" column. POSTED means the stock physically left and the goods receipt
 * and purchase order lines were written back; CANCELLED means the paperwork was
 * abandoned and nothing moved at all. Showing them as the same thing would tell
 * a storekeeper that a shelf is empty when it is not.
 *
 * Every narrowing is a query parameter the API answers - status, vendor,
 * warehouse, the return date window and the one-receipt filter - because a
 * return register runs to years of documents and is never pulled into the
 * browser to be filtered there (master rules 31/36/78).
 *
 * `PurchaseReturnSummary` carries ids, not names, so vendor and warehouse are
 * resolved through the bounded reference projections their own modules publish:
 * one cached lookup per session rather than a request per row.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Ban, ExternalLink, Undo2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

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
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatMoney, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { indexById } from '@/lib/utils'

import { clearSearchKeys } from './procurement-types'
import {
  PurchaseReturnCancelDialog,
  PurchaseReturnDetailDialog,
  PurchaseReturnFormDialog,
  PurchaseReturnPostDialog,
} from './purchase-return-dialogs'
import {
  PURCHASE_RETURN_STATUSES,
  isReturnCancellable,
  isReturnPostable,
  purchaseReturnKeys,
  type PurchaseReturnRow,
} from './purchase-return-model'

/**
 * Every filter this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared - and
 * `clearSearchKeys` writes the whole set in one navigation, because seven
 * `setParam` calls in a handler all resolve against the same stale URL and only
 * the last one survives.
 */
const FILTER_KEYS = [
  'status',
  'vendor_id',
  'warehouse_id',
  'goods_receipt_id',
  'return_from',
  'return_to',
] as const

export default function PurchaseReturnsPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [formOpen, setFormOpen] = useState(false)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [pendingPost, setPendingPost] = useState<PurchaseReturnRow | null>(null)
  const [pendingCancel, setPendingCancel] = useState<PurchaseReturnRow | null>(null)

  /**
   * Raising, posting and cancelling are all `purchase_return.create`.
   *
   * The API declares that same right on all three routes - posting a return is
   * *not* split off behind a separate posting right the way a goods receipt's
   * is (`grn.post`). Gating the post button on anything else would either hide
   * it from people the API will happily let post, or offer it to people who
   * will be refused at the last step.
   */
  const canManage = can(P.PURCHASE_RETURN_CREATE)

  const status = searchParams.get('status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const warehouseId = searchParams.get('warehouse_id') ?? ''
  // Set by a link from elsewhere, or by a bookmark: "what of this delivery has
  // already gone back?". There is no picker for it - a receipt code is not
  // something anyone types from memory - so the chip below is how it is seen
  // and cleared.
  const goodsReceiptId = searchParams.get('goods_receipt_id') ?? ''
  const returnFrom = searchParams.get('return_from') ?? ''
  const returnTo = searchParams.get('return_to') ?? ''

  const list = useListQuery<PurchaseReturnRow>({
    path: '/procurement/purchase-returns',
    queryKey: purchaseReturnKeys.list,
    defaultSortBy: 'return_date',
    defaultSortDir: 'desc',
    // A draft raised this morning and posted this afternoon changes what the
    // warehouse holds, so this list is treated as a document register in flight
    // rather than as master data.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(warehouseId ? { warehouse_id: warehouseId } : {}),
      ...(goodsReceiptId ? { goods_receipt_id: goodsReceiptId } : {}),
      ...(returnFrom ? { return_from: returnFrom } : {}),
      ...(returnTo ? { return_to: returnTo } : {}),
    },
  })

  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  // The same bounded lookup names the "Leaves" column and fills the warehouse
  // filter, so the screen asks the reference endpoint once rather than twice.
  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  const columns = useMemo<ColumnDef<PurchaseReturnRow, unknown>[]>(() => {
    const defs: ColumnDef<PurchaseReturnRow, unknown>[] = [
      {
        // `code`, `return_date`, `status`, `total_amount` and `created_at` are
        // the only fields the API will sort on, so every other header below is
        // fixed rather than sending a 400.
        accessorKey: 'code',
        header: 'Return',
        cell: ({ row }) => (
          // A real button, not just the row's click handler: the row is a <tr>
          // with an onClick and is unreachable from the keyboard on its own.
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              setDetailId(row.original.id)
            }}
            className="rounded-sm font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {row.original.code}
          </button>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        accessorKey: 'return_date',
        header: 'Return date',
        cell: ({ row }) => formatDate(row.original.return_date),
      },
      {
        id: 'vendor',
        header: 'Vendor',
        enableSorting: false,
        cell: ({ row }) => {
          const vendor = vendorById.get(row.original.vendor_id)
          if (!vendor) return <span className="text-muted-foreground">-</span>
          return (
            <div className="max-w-[16rem]">
              <span className="block truncate">{vendor.name}</span>
              <span className="block truncate text-xs text-muted-foreground">{vendor.code}</span>
            </div>
          )
        },
      },
      {
        id: 'warehouse',
        header: 'Leaves',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {warehouseById.get(row.original.warehouse_id)?.code ?? '-'}
          </span>
        ),
      },
      {
        id: 'against',
        header: 'Against',
        enableSorting: false,
        // The goods receipt arrives as an id and no screen addresses a single
        // receipt, so it is not offered as a link. The purchase order has a
        // detail route, and is nullable on the wire.
        cell: ({ row }) =>
          row.original.purchase_order_id ? (
            <Link
              to={`/procurement/purchase-orders/${row.original.purchase_order_id}`}
              className="inline-flex items-center gap-1 text-xs hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              Purchase order
              <ExternalLink className="h-3 w-3" aria-hidden />
            </Link>
          ) : (
            <span className="text-xs text-muted-foreground">No purchase order</span>
          ),
      },
      {
        id: 'debit_note_number',
        header: 'Debit note',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.debit_note_number ? (
            <span className="tabular text-xs">{row.original.debit_note_number}</span>
          ) : (
            <span className="text-xs text-muted-foreground">Not raised</span>
          ),
      },
      {
        id: 'reason',
        header: 'Reason',
        enableSorting: false,
        // Searchable on the server alongside the code and the debit note, which
        // is why it earns a column: "damaged in transit" is how a storekeeper
        // finds the document again.
        cell: ({ row }) => (
          <span className="block max-w-[18rem] truncate" title={row.original.reason}>
            {row.original.reason}
          </span>
        ),
      },
      {
        accessorKey: 'total_amount',
        header: 'Debited',
        meta: { align: 'right' },
        // Each row states its own currency and is formatted in it; nothing on
        // this screen adds two denominations together.
        cell: ({ row }) =>
          formatMoney(row.original.total_amount, { currency: row.original.currency }),
      },
      {
        id: 'settled',
        header: 'Stock',
        enableSorting: false,
        // The column that keeps the two terminal states apart. Posted means the
        // material physically left; cancelled means the document stopped and
        // the material never moved.
        cell: ({ row }) => {
          if (row.original.status === 'POSTED') {
            return (
              <span className="text-xs text-success">
                Left {formatDate(row.original.posted_at)}
              </span>
            )
          }
          if (row.original.status === 'CANCELLED') {
            return <span className="text-xs text-muted-foreground">Abandoned - nothing moved</span>
          }
          return <span className="text-xs text-warning">Not posted - still in stock</span>
        },
      },
    ]

    // Gating is UX only - the API authorises each command independently.
    if (canManage) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <div className="flex items-center justify-end gap-1">
            {isReturnPostable(row.original.status) && (
              <Button
                variant="outline"
                size="sm"
                onClick={(event) => {
                  event.stopPropagation()
                  setPendingPost(row.original)
                }}
              >
                <Undo2 />
                Post
              </Button>
            )}
            {isReturnCancellable(row.original.status) && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Cancel ${row.original.code}`}
                onClick={(event) => {
                  event.stopPropagation()
                  setPendingCancel(row.original)
                }}
              >
                <Ban />
              </Button>
            )}
          </div>
        ),
      })
    }

    return defs
  }, [canManage, vendorById, warehouseById])

  const newReturnButton = canManage ? (
    <Button onClick={() => setFormOpen(true)}>
      <Undo2 />
      New return
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Purchase returns"
        description="Material going back to the vendor. A return is raised against a posted goods receipt and takes the stock out only when it is posted - which cannot be undone."
        actions={newReturnButton}
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
        searchPlaceholder="Search return code, debit note or reason..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setDetailId(row.id)}
        emptyTitle="No purchase returns found"
        emptyDescription="Nothing matches the current filters. A return always starts from a posted goods receipt - raise one to send material back to the vendor."
        emptyAction={newReturnButton}
        toolbar={
          <>
            <Select
              value={status || SELECT_ALL}
              onValueChange={(value) =>
                setParam('status', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All statuses</SelectItem>
                {PURCHASE_RETURN_STATUSES.map((value) => (
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
                id="return-vendor-filter"
                className="w-full sm:w-auto"
                label="Vendor"
                hideLabel
                value={vendorId || null}
                onChange={(value) => setParam('vendor_id', value ?? undefined)}
                placeholder="All vendors"
              />
            )}

            {/* Only offered to accounts that may read the warehouse master;
                without that right the option list would be empty and the filter
                would look broken rather than unavailable. */}
            {can(P.WAREHOUSE_VIEW) && (
              <Select
                value={warehouseId || SELECT_ALL}
                onValueChange={(value) =>
                  setParam('warehouse_id', value === SELECT_ALL ? undefined : value)
                }
              >
                <SelectTrigger className="w-full sm:w-[200px]" aria-label="Filter by warehouse">
                  <SelectValue placeholder="All warehouses" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELECT_ALL}>All warehouses</SelectItem>
                  {(warehouses.data ?? []).map((warehouse) => (
                    <SelectItem key={warehouse.id} value={warehouse.id}>
                      {warehouse.code} - {warehouse.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            <DateRangeFilter
              id="return"
              from={returnFrom}
              to={returnTo}
              onFromChange={(value) => setParam('return_from', value || undefined)}
              onToChange={(value) => setParam('return_to', value || undefined)}
              fromInputProps={{ max: returnTo || undefined }}
              toInputProps={{ min: returnFrom || undefined }}
            />

            {goodsReceiptId && (
              <Badge variant="secondary" className="h-9 gap-1.5 px-3">
                One goods receipt
                <button
                  type="button"
                  onClick={() => setParam('goods_receipt_id', undefined)}
                  aria-label="Stop filtering to one goods receipt"
                  className="rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </Badge>
            )}

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => clearSearchKeys(setSearchParams, FILTER_KEYS)}
              >
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />

      {canManage && <PurchaseReturnFormDialog open={formOpen} onOpenChange={setFormOpen} />}

      <PurchaseReturnDetailDialog
        returnId={detailId}
        onClose={() => setDetailId(null)}
        canAllocate={can(P.VENDOR_INVOICE_VIEW) && can(P.PAYMENT_APPROVE)}
        // Handed over only when the account may act. The dialog offers no
        // command it was not given, so a viewer reads the return and nothing
        // more.
        onPost={
          canManage
            ? (row) => {
                setDetailId(null)
                setPendingPost(row)
              }
            : undefined
        }
        onCancel={
          canManage
            ? (row) => {
                setDetailId(null)
                setPendingCancel(row)
              }
            : undefined
        }
      />

      <PurchaseReturnPostDialog
        purchaseReturn={pendingPost}
        onOpenChange={(open) => !open && setPendingPost(null)}
      />

      <PurchaseReturnCancelDialog
        purchaseReturn={pendingCancel}
        onOpenChange={(open) => !open && setPendingCancel(null)}
      />
    </>
  )
}
