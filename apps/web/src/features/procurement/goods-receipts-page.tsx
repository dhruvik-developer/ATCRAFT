/**
 * Goods receipt list, and the door into receiving against an order.
 *
 * A receipt is the moment purchased material becomes stock, so the fact this
 * list leads with is whether it has actually been posted. A drafted-but-unposted
 * receipt is material physically in the building that the system does not yet
 * own, and that gap is what this screen exists to make visible.
 *
 * "Waiting on QC" is a filter (`qc_pending_only`) rather than a per-row badge:
 * `GoodsReceiptSummary` carries no QC flag, because the answer lives on the
 * receipt's lines. Filtering asks the API the question; a badge would have had
 * to invent it.
 *
 * Vendor and warehouse arrive as ids, so both are named through the bounded
 * reference lookups the API publishes for pickers.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import { ExternalLink, PackageCheck, Upload, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { useWarehouseOptions } from '@/features/warehouses/warehouse-model'
import { useListQuery } from '@/hooks/use-list-query'
import { formatDate, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { api } from '@/lib/api-client'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { indexById } from '@/lib/utils'

import { GrnReceiveDialog } from './grn-receive-dialog'
import {
  GRN_STATUSES,
  clearSearchKeys,
  type GoodsReceiptPostResult,
  type GoodsReceiptRow,
} from './procurement-types'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/** Every filter this screen owns, cleared as one write rather than five. */
const FILTER_KEYS = [
  'status',
  'vendor_id',
  'received_from',
  'received_to',
  'qc_pending_only',
] as const

export default function GoodsReceiptsPage() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [receiveOpen, setReceiveOpen] = useState(false)
  const [pendingPost, setPendingPost] = useState<GoodsReceiptRow | null>(null)

  const canReceive = can(P.GRN_CREATE) || can(P.GRN_POST)
  const canPost = can(P.GRN_POST)

  const status = searchParams.get('status') ?? ''
  const vendorId = searchParams.get('vendor_id') ?? ''
  const receivedFrom = searchParams.get('received_from') ?? ''
  const receivedTo = searchParams.get('received_to') ?? ''
  const qcPendingOnly = searchParams.get('qc_pending_only') === 'true'

  const list = useListQuery<GoodsReceiptRow>({
    path: '/procurement/goods-receipts',
    queryKey: queryKeys.procurement.grns,
    defaultSortBy: 'receipt_date',
    defaultSortDir: 'desc',
    // Receipts are posted throughout a shift; the gate needs today's picture,
    // not one cached when the tab was opened.
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(vendorId ? { vendor_id: vendorId } : {}),
      ...(receivedFrom ? { received_from: receivedFrom } : {}),
      ...(receivedTo ? { received_to: receivedTo } : {}),
      ...(qcPendingOnly ? { qc_pending_only: true } : {}),
    },
  })

  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const warehouses = useWarehouseOptions(can(P.WAREHOUSE_VIEW))
  const warehouseById = useMemo(() => indexById(warehouses.data), [warehouses.data])

  const { setParam } = list
  const filterCount =
    (status ? 1 : 0) +
    (vendorId ? 1 : 0) +
    (receivedFrom ? 1 : 0) +
    (receivedTo ? 1 : 0) +
    (qcPendingOnly ? 1 : 0)

  // Posting answers with a GoodsReceiptPostResult - what it did, not the
  // receipt - so the receipt is read from `result.receipt`. Reading `code` off
  // the envelope directly is what used to make the toast say "undefined".
  const postReceipt = useMutation({
    mutationFn: (receipt: GoodsReceiptRow) =>
      api.post<GoodsReceiptPostResult>(`/procurement/goods-receipts/${receipt.id}/post`),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.procurement.all })
      // Posting writes stock movements, so balances and availability are
      // dropped rather than left to expire (master rules 34).
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.quality.all })
      const orderNow = `The order is now ${humanise(result.purchase_order_status).toLowerCase()}.`
      // Whether an inspection actually exists is `qc_inspections_created`, not
      // an assumption: posting flags the lines, and the quality module raises
      // the inspection when it is there to raise it.
      const held =
        result.qc_hold_line_count > 0
          ? `${result.qc_hold_line_count} line(s) went to QC hold rather than free stock, ${
              result.qc_inspections_created
                ? 'and an inspection has been raised.'
                : 'and quality will pick them up.'
            }`
          : 'The stock is available.'
      toast.success('Goods receipt posted', `${result.receipt.code} is posted. ${held} ${orderNow}`)
      setPendingPost(null)
    },
  })

  const columns = useMemo<ColumnDef<GoodsReceiptRow, unknown>[]>(() => {
    const defs: ColumnDef<GoodsReceiptRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Receipt',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
      },
      {
        accessorKey: 'receipt_date',
        header: 'Received',
        cell: ({ row }) => formatDate(row.original.receipt_date),
      },
      {
        id: 'purchase_order',
        header: 'Against',
        enableSorting: false,
        // The row carries the order's id but not its code, so the link is
        // labelled by what it does rather than by a code the API did not send.
        cell: ({ row }) => (
          <Link
            to={`/procurement/purchase-orders/${row.original.purchase_order_id}`}
            className="inline-flex items-center gap-1 hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            Purchase order
            <ExternalLink className="h-3 w-3" aria-hidden />
          </Link>
        ),
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
        header: 'Into',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {warehouseById.get(row.original.warehouse_id)?.code ?? '-'}
          </span>
        ),
      },
      {
        id: 'delivery_note',
        header: 'Delivery note',
        enableSorting: false,
        // The vendor's challan number, and the invoice it came in on. Both are
        // what a storekeeper reconciles the pile against.
        cell: ({ row }) =>
          row.original.vendor_delivery_note || row.original.vendor_invoice_number ? (
            <span className="block max-w-[12rem]">
              <span className="block truncate">{row.original.vendor_delivery_note ?? '-'}</span>
              {row.original.vendor_invoice_number && (
                <span className="block truncate text-xs text-muted-foreground">
                  Invoice {row.original.vendor_invoice_number}
                </span>
              )}
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      },
      {
        id: 'received_quantity',
        header: 'Received',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.total_received_quantity),
      },
      {
        id: 'accepted_quantity',
        header: 'Accepted',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="text-success">
            {formatQuantity(row.original.total_accepted_quantity)}
          </span>
        ),
      },
      {
        id: 'rejected_quantity',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          toDecimal(row.original.total_rejected_quantity).isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="font-medium text-destructive">
              {formatQuantity(row.original.total_rejected_quantity)}
            </span>
          ),
      },
      {
        id: 'posted',
        header: 'Posted',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.posted_at ? (
            <span className="text-xs text-muted-foreground">
              {formatDate(row.original.posted_at)}
            </span>
          ) : (
            <span className="text-xs text-warning">Not posted - no stock yet</span>
          ),
      },
    ]

    // Gating is UX only - the API authorises the post independently.
    if (canPost) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) =>
          row.original.status === 'DRAFT' ? (
            <Button variant="outline" size="sm" onClick={() => setPendingPost(row.original)}>
              <Upload />
              Post
            </Button>
          ) : null,
      })
    }

    return defs
  }, [canPost, vendorById, warehouseById])

  const receiveButton = canReceive ? (
    <Button onClick={() => setReceiveOpen(true)}>
      <PackageCheck />
      Receive against PO
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Goods receipts"
        description="What has arrived from vendors. Material becomes stock when a receipt is posted - anything needing inspection lands in QC hold rather than free stock."
        actions={receiveButton}
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
        searchPlaceholder="Search receipt code, delivery note or invoice..."
        getRowId={(row) => row.id}
        emptyTitle="No goods receipts found"
        emptyDescription="Nothing matches the current filters. Widen the date window, or receive against an open order."
        emptyAction={receiveButton}
        toolbar={
          <>
            <Select
              value={status || ALL}
              onValueChange={(value) => setParam('status', value === ALL ? undefined : value)}
            >
              <SelectTrigger className="w-[170px]" aria-label="Filter by status">
                <SelectValue placeholder="All statuses" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All statuses</SelectItem>
                {GRN_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <VendorPicker
              id="grn-vendor-filter"
              label="Vendor"
              hideLabel
              value={vendorId || null}
              onChange={(value) => setParam('vendor_id', value ?? undefined)}
              placeholder="All vendors"
            />

            <div className="flex items-center gap-1.5">
              <Label htmlFor="grn-received-from" className="text-xs text-muted-foreground">
                From
              </Label>
              <Input
                id="grn-received-from"
                type="date"
                value={receivedFrom}
                onChange={(event) => setParam('received_from', event.target.value || undefined)}
                className="w-[9.5rem]"
              />
              <Label htmlFor="grn-received-to" className="text-xs text-muted-foreground">
                To
              </Label>
              <Input
                id="grn-received-to"
                type="date"
                value={receivedTo}
                onChange={(event) => setParam('received_to', event.target.value || undefined)}
                className="w-[9.5rem]"
              />
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="grn-qc-pending"
                checked={qcPendingOnly}
                onCheckedChange={(checked) =>
                  setParam('qc_pending_only', checked === true ? 'true' : undefined)
                }
              />
              <Label htmlFor="grn-qc-pending" className="font-normal">
                Waiting on QC
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

      {canReceive && <GrnReceiveDialog open={receiveOpen} onOpenChange={setReceiveOpen} />}

      <ConfirmDialog
        open={Boolean(pendingPost)}
        onOpenChange={(open) => !open && setPendingPost(null)}
        title={pendingPost ? `Post ${pendingPost.code}?` : ''}
        // What posting guarantees: the movements, the order's status, and that
        // anything requiring inspection lands in QC hold instead of free stock.
        // Whether an inspection document is raised on top of that is the quality
        // module's to answer, and the result says which happened - so it is
        // reported afterwards rather than promised here.
        description="Posting writes the stock movements and advances the purchase order. Anything requiring inspection lands in QC hold rather than free stock. A posted receipt cannot be edited."
        confirmLabel="Post receipt"
        loading={postReceipt.isPending}
        onConfirm={() => pendingPost && postReceipt.mutate(pendingPost)}
      />
    </>
  )
}
