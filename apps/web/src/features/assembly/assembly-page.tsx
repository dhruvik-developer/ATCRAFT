/**
 * Assembly orders.
 *
 * The list is server-driven like every other register in the application; the
 * order itself opens in a panel over it, because an assembly is a short document
 * and sending someone to a separate page to read six fields loses their place in
 * a list they are working down.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { X } from 'lucide-react'
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
import { ItemPicker } from '@/features/inventory/pickers'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatDate, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { AssemblyDetailDialog } from './assembly-detail-dialog'
import { ASSEMBLY_STATUSES, assemblyKeys, type AssemblyOrderRow } from './assembly-model'

/** Every URL key the toolbar owns, so "Clear" can drop them in one write. */
const FILTER_KEYS = ['status', 'item_id', 'date_from', 'date_to'] as const

export default function AssemblyPage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)

  // Gating affordances only - the API authorises every write itself. Completion
  // is a different permission from the rest of the order's writes.
  const canManage = can(P.ASSEMBLY_ORDER_MANAGE)
  const canComplete = can(P.ASSEMBLY_COMPLETE)

  const status = searchParams.get('status') ?? ''
  const itemId = searchParams.get('item_id') ?? ''
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''

  const list = useListQuery<AssemblyOrderRow>({
    path: '/assembly',
    queryKey: assemblyKeys.list,
    defaultSortBy: 'planned_start',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(status ? { status } : {}),
      ...(itemId ? { item_id: itemId } : {}),
      // The date window filters on the planned start: the endpoint takes
      // `planned_from` / `planned_to`, and the `date_from` / `date_to` it was
      // sent before were ignored, so the range picker did nothing.
      ...(dateFrom ? { planned_from: dateFrom } : {}),
      ...(dateTo ? { planned_to: dateTo } : {}),
    },
  })

  const { setParam } = list

  /**
   * Clearing is one write, not four.
   *
   * `setSearchParams` resolves its updater against the `searchParams` of the
   * render it was called from and navigates immediately, so four calls in one
   * handler all start from the same location and only the last navigation
   * survives - the status, item and from-date filters stayed exactly where they
   * were while the button claimed to have cleared them.
   */
  const clearFilters = () =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        next.delete('page')
        return next
      },
      { replace: true },
    )

  const filterCount = (status ? 1 : 0) + (itemId ? 1 : 0) + (dateFrom ? 1 : 0) + (dateTo ? 1 : 0)

  const columns = useMemo<ColumnDef<AssemblyOrderRow, unknown>[]>(
    () => [
      {
        accessorKey: 'code',
        header: 'Order',
        // A button rather than a link: the order opens in a panel, and this
        // screen has to stay usable from the keyboard alone.
        cell: ({ row }) => (
          <button
            type="button"
            className="font-medium hover:underline focus-visible:outline-none focus-visible:underline"
            onClick={(event) => {
              event.stopPropagation()
              setOpenOrderId(row.original.id)
            }}
          >
            {row.original.code}
          </button>
        ),
      },
      {
        id: 'item',
        header: 'Item',
        enableSorting: false,
        // The row carries an item id and nothing else - no code, no name.
        cell: ({ row }) => (
          <Link
            to={`/items/${row.original.item_id}`}
            className="font-mono text-xs hover:underline"
            onClick={(event) => event.stopPropagation()}
            title="Open this item"
          >
            {row.original.item_id.slice(0, 8)}
          </Link>
        ),
      },
      {
        accessorKey: 'planned_quantity',
        header: 'Planned',
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.planned_quantity),
      },
      {
        // `assembled_quantity` is not one of the endpoint's sortable fields
        // (code, status, planned_start, planned_end, actual_end,
        // planned_quantity, created_at), and an unknown `sort_by` is a hard
        // validation error rather than an ignored parameter - so offering the
        // header emptied the table with a toast.
        id: 'assembled_quantity',
        header: 'Assembled',
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => formatQuantity(row.original.assembled_quantity),
      },
      {
        // Output that failed inspection is not just a smaller yield - it is
        // material to be scrapped or reworked, so it belongs in the scan.
        id: 'rejected_quantity',
        header: 'Rejected',
        enableSorting: false,
        meta: { align: 'right' },
        // Compared as a decimal, not against the literal "0": a NUMERIC(18,4)
        // column serialises as "0.0000", so a string comparison marked every
        // untouched order as having rejected output.
        cell: ({ row }) =>
          toDecimal(row.original.rejected_quantity).isZero() ? (
            <span className="text-muted-foreground">-</span>
          ) : (
            <span className="font-medium text-destructive">
              {formatQuantity(row.original.rejected_quantity)}
            </span>
          ),
      },
      {
        id: 'status',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="flex items-center gap-1.5">
            <StatusBadge status={row.original.status} />
            {row.original.requires_qc && !row.original.qc_inspection_id && (
              <Badge variant="muted">QC</Badge>
            )}
          </span>
        ),
      },
      {
        accessorKey: 'planned_start',
        header: 'Planned start',
        cell: ({ row }) => formatDate(row.original.planned_start),
      },
      {
        accessorKey: 'planned_end',
        header: 'Planned end',
        cell: ({ row }) => formatDate(row.original.planned_end),
      },
      {
        id: 'assembly_line',
        header: 'Line',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.assembly_line ?? '-'}</span>
        ),
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={(event) => {
              event.stopPropagation()
              setOpenOrderId(row.original.id)
            }}
          >
            Open
          </Button>
        ),
      },
    ],
    [],
  )

  return (
    <>
      <PageHeader
        title="Assembly orders"
        description="Kits built from issued components. Where a component is serial tracked, the serials that went into each build are recorded against the order."
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
        searchPlaceholder="Search order code or item..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setOpenOrderId(row.id)}
        emptyTitle="No assembly orders found"
        emptyDescription="Nothing matches the current filters. Widen the date window or clear a filter."
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
                {ASSEMBLY_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {humanise(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <ItemPicker
              id="assembly-item-filter"
              label="Item"
              hideLabel
              value={itemId || null}
              onChange={(value) => setParam('item_id', value ?? undefined)}
              placeholder="All items"
            />

            <DateRangeFilter
              id="assembly-date"
              from={dateFrom}
              to={dateTo}
              onFromChange={(value) => setParam('date_from', value || undefined)}
              onToChange={(value) => setParam('date_to', value || undefined)}
            />

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {openOrderId && (
        <AssemblyDetailDialog
          open
          onOpenChange={(open) => !open && setOpenOrderId(null)}
          orderId={openOrderId}
          canManage={canManage}
          canComplete={canComplete}
        />
      )}
    </>
  )
}
